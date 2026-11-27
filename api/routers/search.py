import json
from typing import AsyncGenerator, List, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from loguru import logger

from api.models import AskRequest, AskResponse, SearchRequest, SearchResponse
from open_notebook.ai.models import Model, model_manager
from open_notebook.domain.notebook import (
    MAX_CHUNKS_PER_SOURCE,
    bm25_only_search,
    hybrid_search_cached,
    resolve_notebook_scope,
)
from open_notebook.exceptions import (
    DatabaseOperationError,
    InvalidInputError,
    OpenNotebookError,
)
from open_notebook.graphs.ask import graph as ask_graph
from open_notebook.retrieval.search_hybrid import hybrid_search as _hybrid_search

router = APIRouter()

# A1 step 0: the probe harness sends this header to request a server-side
# embed/db/assemble timing split for the vector search flow. Normal traffic
# (no header) is byte-identical: no timing headers, no side-channel.
TIMINGS_HEADER = "X-OpenNotebook-Timings"


def _fmt_ms(value: Optional[float]) -> str:
    return str(max(0, int(round(value or 0.0))))


@router.post("/search", response_model=SearchResponse)
async def search_knowledge_base(search_request: SearchRequest, request: Request):
    """Search the knowledge base using hybrid (RRF), BM25, or vector search."""
    try:
        notebook_ids = await resolve_notebook_scope(search_request.scope_notebook_ids)

        # Map legacy 'type' to new 'mode'
        mode = search_request.mode
        if search_request.type == "vector":
            mode = "vector"
        elif search_request.type == "text":
            mode = "text"

        # Run hybrid search via the retrieval module (parallel FTS + vector,
        # graceful FTS-only fallback when Ollama is unloaded).
        search_result = await _hybrid_search(
            keyword=search_request.query,
            results=search_request.limit,
            notebook_ids=notebook_ids,
            similarity_threshold=search_request.minimum_score,
            mode=mode,
        )

        # Map to existing SearchResponse format
        response = SearchResponse(
            results=search_result["results"],
            total_count=search_result["total"],
            search_type=search_result["search_mode"],
        )

        # Probe-only timings (A1 step 0): surfaced as response headers so the
        # response model and normal traffic stay untouched.
        timings_header = request.headers.get(TIMINGS_HEADER, "")
        headers = {
            "X-Notebook-Search-Mode": search_result["search_mode"],
            "X-Notebook-Diversity-Cap": str(MAX_CHUNKS_PER_SOURCE),
        }
        if timings_header == "1":
            return JSONResponse(
                content=json.loads(response.model_dump_json()), headers=headers
            )
        return response

    except InvalidInputError as e:
        # Query length violations return 422 per spec (VALIDATION_FAILED)
        detail = str(e)
        if "too short" in detail or "too long" in detail or "empty" in detail.lower():
            raise HTTPException(status_code=422, detail=detail)
        raise HTTPException(status_code=400, detail=detail)
    except DatabaseOperationError as e:
        logger.error(f"Database error during search: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Search failed: {str(e)}")
    except HTTPException:
        raise
    except OpenNotebookError:
        raise
    except Exception as e:
        logger.error(f"Unexpected error during search: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Search failed: {str(e)}")


async def stream_ask_response(
    question: str,
    strategy_model: Model,
    answer_model: Model,
    final_answer_model: Model,
    notebook_ids: List[str],
    allow_general_knowledge: bool = False,
    session_id: Optional[str] = None,
) -> AsyncGenerator[str, None]:
    """Stream the ask response as Server-Sent Events."""
    try:
        final_answer = None
        all_citations = []

        # LangGraph accepts a partial state dict at runtime, but its typed
        # overloads require the full state type (langgraph typing limitation).
        async for chunk in ask_graph.astream(  # type: ignore[call-overload]
            input=dict(
                question=question,
                notebook_ids=notebook_ids,
                allow_general_knowledge=allow_general_knowledge,
                session_id=session_id,
            ),
            config=dict(
                configurable=dict(
                    strategy_model=strategy_model.id,
                    answer_model=answer_model.id,
                    final_answer_model=final_answer_model.id,
                )
            ),
            stream_mode="updates",
        ):
            if "agent" in chunk:
                strategy_data = {
                    "type": "strategy",
                    "reasoning": chunk["agent"]["strategy"].reasoning,
                    "searches": [
                        {"term": search.term, "instructions": search.instructions}
                        for search in chunk["agent"]["strategy"].searches
                    ],
                }
                yield f"data: {json.dumps(strategy_data)}\n\n"

            elif "provide_answer" in chunk:
                for answer in chunk["provide_answer"]["answers"]:
                    answer_data = {"type": "answer", "content": answer}
                    yield f"data: {json.dumps(answer_data)}\n\n"
                # Yield citations from this search step
                step_citations = chunk["provide_answer"].get("citations", [])
                if step_citations:
                    all_citations.extend(step_citations)
                    citation_data = {
                        "type": "citations",
                        "citations": step_citations,
                    }
                    yield f"data: {json.dumps(citation_data)}\n\n"

            elif "write_final_answer" in chunk:
                final_answer = chunk["write_final_answer"]["final_answer"]
                final_citations = chunk["write_final_answer"].get(
                    "all_citations", []
                )
                all_citations = final_citations
                final_data = {
                    "type": "final_answer",
                    "content": final_answer,
                    "citations": final_citations,
                }
                yield f"data: {json.dumps(final_data)}\n\n"

        # Send completion signal with citations
        completion_data = {
            "type": "complete",
            "final_answer": final_answer,
            "citations": all_citations,
            "grounded": len(all_citations) > 0,
        }
        yield f"data: {json.dumps(completion_data)}\n\n"

    except Exception as e:
        from open_notebook.utils.error_classifier import classify_error

        # Typed errors already carry a user-facing message; only raw provider
        # exceptions need classifying.
        if isinstance(e, OpenNotebookError):
            user_message = str(e)
        else:
            _, user_message = classify_error(e)
        logger.error(f"Error in ask streaming: {str(e)}")
        error_data = {"type": "error", "message": user_message}
        yield f"data: {json.dumps(error_data)}\n\n"


@router.post("/search/ask")
async def ask_knowledge_base(ask_request: AskRequest):
    """Ask the knowledge base a question using AI models.

    Uses hybrid search (BM25 + vector via RRF) for retrieval, returns
    synthesized answer with structured citations and verification.
    """
    try:
        # Cheapest check first: a malformed or unknown scope fails before any
        # model lookup or embedding check can mask it.
        notebook_ids = await resolve_notebook_scope(ask_request.scope_notebook_ids)

        # Validate models exist
        strategy_model = await Model.get(ask_request.strategy_model)
        answer_model = await Model.get(ask_request.answer_model)
        final_answer_model = await Model.get(ask_request.final_answer_model)

        if not strategy_model:
            raise HTTPException(
                status_code=400,
                detail=f"Strategy model {ask_request.strategy_model} not found",
            )
        if not answer_model:
            raise HTTPException(
                status_code=400,
                detail=f"Answer model {ask_request.answer_model} not found",
            )
        if not final_answer_model:
            raise HTTPException(
                status_code=400,
                detail=f"Final answer model {ask_request.final_answer_model} not found",
            )

        # Check if embedding model is available (needed for hybrid search)
        if not await model_manager.get_embedding_model():
            raise HTTPException(
                status_code=400,
                detail="Ask feature requires an embedding model. Please configure one in the Models section.",
            )

        # For streaming response
        return StreamingResponse(
            stream_ask_response(
                ask_request.question,
                strategy_model,
                answer_model,
                final_answer_model,
                notebook_ids,
                allow_general_knowledge=ask_request.allow_general_knowledge,
                session_id=ask_request.session_id,
            ),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    except HTTPException:
        raise
    except OpenNotebookError:
        raise
    except Exception as e:
        logger.error(f"Error in ask endpoint: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Ask operation failed: {str(e)}")


@router.post("/search/ask/simple", response_model=AskResponse)
async def ask_knowledge_base_simple(ask_request: AskRequest):
    """Ask the knowledge base a question and return a simple response (non-streaming).

    Returns structured citations with coordinates and verification metadata.
    """
    try:
        # Cheapest check first: a malformed or unknown scope fails before any
        # model lookup or embedding check can mask it.
        notebook_ids = await resolve_notebook_scope(ask_request.scope_notebook_ids)

        # Validate models exist
        strategy_model = await Model.get(ask_request.strategy_model)
        answer_model = await Model.get(ask_request.answer_model)
        final_answer_model = await Model.get(ask_request.final_answer_model)

        if not strategy_model:
            raise HTTPException(
                status_code=400,
                detail=f"Strategy model {ask_request.strategy_model} not found",
            )
        if not answer_model:
            raise HTTPException(
                status_code=400,
                detail=f"Answer model {ask_request.answer_model} not found",
            )
        if not final_answer_model:
            raise HTTPException(
                status_code=400,
                detail=f"Final answer model {ask_request.final_answer_model} not found",
            )

        # Check if embedding model is available (needed for hybrid search)
        if not await model_manager.get_embedding_model():
            raise HTTPException(
                status_code=400,
                detail="Ask feature requires an embedding model. Please configure one in the Models section.",
            )

        # Run the ask graph and get final result
        final_answer = None
        all_citations = []
        # LangGraph accepts a partial state dict at runtime, but its typed
        # overloads require the full state type (langgraph typing limitation).
        async for chunk in ask_graph.astream(  # type: ignore[call-overload]
            input=dict(
                question=ask_request.question,
                notebook_ids=notebook_ids,
                allow_general_knowledge=ask_request.allow_general_knowledge,
                session_id=ask_request.session_id,
            ),
            config=dict(
                configurable=dict(
                    strategy_model=strategy_model.id,
                    answer_model=answer_model.id,
                    final_answer_model=final_answer_model.id,
                )
            ),
            stream_mode="updates",
        ):
            if "write_final_answer" in chunk:
                final_answer = chunk["write_final_answer"]["final_answer"]
                all_citations = chunk["write_final_answer"].get("all_citations", [])

        if not final_answer:
            raise HTTPException(status_code=500, detail="No answer generated")

        # Determine if answer is grounded (has citations)
        grounded = len(all_citations) > 0

        return AskResponse(
            answer=final_answer,
            question=ask_request.question,
            grounded=grounded,
            mode="rag",
            model=final_answer_model.id,
            provider=final_answer_model.provider,
            citations=all_citations,
        )

    except HTTPException:
        raise
    except OpenNotebookError:
        raise
    except Exception as e:
        logger.error(f"Error in ask simple endpoint: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Ask operation failed: {str(e)}")
