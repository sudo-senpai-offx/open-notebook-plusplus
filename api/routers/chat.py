import asyncio
import traceback
from typing import Any, Dict, List, Optional
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Query
from langchain_core.runnables import RunnableConfig
from loguru import logger
from pydantic import BaseModel, Field

from api.routers._chat_shared import (
    ChatMessage,
    SuccessResponse,
    extract_chat_messages,
    get_session_or_404,
)
from open_notebook.database.repository import ensure_record_id, repo_query
from open_notebook.domain.notebook import (
    ChatSession,
    Notebook,
    persist_chat_messages,
)
from open_notebook.exceptions import (
    NotFoundError,
    OpenNotebookError,
)
from open_notebook.graphs.chat import graph as chat_graph
from open_notebook.utils import token_count
from open_notebook.utils.context_builder import (
    _ensure_prefix,
    build_notebook_context,
    build_relevant_context,
)
from open_notebook.utils.graph_utils import (
    get_session_message_count,
    invoke_chat_turn,
)

router = APIRouter()


# Request/Response models
class CreateSessionRequest(BaseModel):
    notebook_id: str = Field(..., description="Notebook ID to create session for")
    title: Optional[str] = Field(None, description="Optional session title")
    model_override: Optional[str] = Field(
        None, description="Optional model override for this session"
    )


class UpdateSessionRequest(BaseModel):
    title: Optional[str] = Field(None, description="New session title")
    model_override: Optional[str] = Field(
        None, description="Model override for this session"
    )


class ChatSessionResponse(BaseModel):
    id: str = Field(..., description="Session ID")
    title: str = Field(..., description="Session title")
    notebook_id: Optional[str] = Field(None, description="Notebook ID")
    created: str = Field(..., description="Creation timestamp")
    updated: str = Field(..., description="Last update timestamp")
    message_count: Optional[int] = Field(
        None, description="Number of messages in session"
    )
    model_override: Optional[str] = Field(
        None, description="Model override for this session"
    )


class ChatSessionWithMessagesResponse(ChatSessionResponse):
    messages: List[ChatMessage] = Field(
        default_factory=list, description="Session messages"
    )


class ExecuteChatRequest(BaseModel):
    session_id: str = Field(..., description="Chat session ID")
    message: str = Field(..., description="User message content")
    context: Dict[str, Any] = Field(
        ..., description="Chat context with sources and notes"
    )
    model_override: Optional[str] = Field(
        None, description="Optional model override for this message"
    )


class ExecuteChatResponse(BaseModel):
    session_id: str = Field(..., description="Session ID")
    messages: List[ChatMessage] = Field(..., description="Updated message list")


class BuildContextRequest(BaseModel):
    notebook_id: str = Field(..., description="Notebook ID")
    context_config: Dict[str, Any] = Field(..., description="Context configuration")
    # The user's question. Present it and the response is built from the
    # passages that actually match it rather than from whole documents.
    # Optional: without it the endpoint behaves exactly as before.
    query: Optional[str] = Field(
        default=None,
        description="Question to retrieve against; omit for whole-document context",
    )
    retrieval_limit: int = Field(
        default=8, ge=1, le=32, description="Maximum passages to retrieve"
    )


class BuildContextResponse(BaseModel):
    context: Dict[str, Any] = Field(..., description="Built context data")
    token_count: int = Field(..., description="Estimated token count")
    char_count: int = Field(..., description="Character count")
    # How the context was assembled: "retrieval", "empty" or "documents".
    # Surfaced rather than logged because a student asking a question and
    # getting an ungrounded answer deserves to know why.
    retrieval: Optional[Dict[str, Any]] = Field(
        default=None, description="Retrieval outcome, when a query was given"
    )


@router.get("/chat/sessions", response_model=List[ChatSessionResponse])
async def get_sessions(notebook_id: str = Query(..., description="Notebook ID")):
    """Get all chat sessions for a notebook."""
    try:
        # Get notebook to verify it exists
        notebook = await Notebook.get(notebook_id)
        if not notebook:
            raise HTTPException(status_code=404, detail="Notebook not found")

        # Get sessions for this notebook
        sessions_list = await notebook.get_chat_sessions()

        results = []
        for session in sessions_list:
            session_id = str(session.id)

            # Get message count from LangGraph state
            msg_count = await get_session_message_count(chat_graph, session_id)

            results.append(
                ChatSessionResponse(
                    id=session.id or "",
                    title=session.title or "Untitled Session",
                    notebook_id=notebook_id,
                    created=str(session.created),
                    updated=str(session.updated),
                    message_count=msg_count,
                    model_override=getattr(session, "model_override", None),
                )
            )

        return results
    except NotFoundError:
        raise HTTPException(status_code=404, detail="Notebook not found")
    except HTTPException:
        raise
    except OpenNotebookError:
        raise
    except Exception as e:
        logger.error(f"Error fetching chat sessions: {str(e)}")
        raise HTTPException(
            status_code=500, detail=f"Error fetching chat sessions: {str(e)}"
        )


@router.post("/chat/sessions", response_model=ChatSessionResponse)
async def create_session(request: CreateSessionRequest):
    """Create a new chat session."""
    try:
        # Verify notebook exists
        notebook = await Notebook.get(request.notebook_id)
        if not notebook:
            raise HTTPException(status_code=404, detail="Notebook not found")

        # Create new session
        session = ChatSession(
            title=request.title
            or f"Chat Session {asyncio.get_event_loop().time():.0f}",
            model_override=request.model_override,
        )
        await session.save()

        # Relate session to notebook
        await session.relate_to_notebook(request.notebook_id)

        return ChatSessionResponse(
            id=session.id or "",
            title=session.title or "",
            notebook_id=request.notebook_id,
            created=str(session.created),
            updated=str(session.updated),
            message_count=0,
            model_override=session.model_override,
        )
    except NotFoundError:
        raise HTTPException(status_code=404, detail="Notebook not found")
    except HTTPException:
        raise
    except OpenNotebookError:
        raise
    except Exception as e:
        logger.error(f"Error creating chat session: {str(e)}")
        raise HTTPException(
            status_code=500, detail=f"Error creating chat session: {str(e)}"
        )


@router.get(
    "/chat/sessions/{session_id}", response_model=ChatSessionWithMessagesResponse
)
async def get_session(session_id: str):
    """Get a specific session with its messages."""
    try:
        # Get session (normalizes the ID and 404s if missing)
        full_session_id, session = await get_session_or_404(session_id)

        # Get session state from LangGraph to retrieve messages
        # Use sync get_state() in a thread since SqliteSaver doesn't support async
        thread_state = await asyncio.to_thread(
            chat_graph.get_state,
            config=RunnableConfig(configurable={"thread_id": full_session_id}),
        )

        # The durable transcript first; the checkpoint only as a fallback.
        #
        # They disagree for exactly one reason: a conversation that
        # predates the chat_message table is still only in the
        # checkpoint. So an empty table is read as "not migrated yet",
        # not as "no messages" - otherwise every old session would come
        # back empty and the user would think the history was lost.
        #
        # Backfilling on read means an old conversation becomes syncable
        # the first time it is opened, with no migration step.
        # Tolerates the table not existing yet. An app update can land
        # before its migration has run - the two ship separately and the
        # phone pulls code and migrates on next start - and a missing
        # table must degrade to "no stored transcript", not a 500 on the
        # one page the user is trying to read.
        stored = []
        try:
            stored = await repo_query(
                "SELECT * FROM chat_message WHERE session = $sid ORDER BY seq ASC, created ASC",
                {"sid": ensure_record_id(full_session_id)},
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                f"chat_message unavailable for {full_session_id}, reading the "
                f"checkpoint instead: {type(exc).__name__}: {exc}"
            )

        messages: list[ChatMessage] = []
        if stored:
            # Filtered rather than trusted: a row missing `id` or
            # `content` would raise inside the comprehension and take the
            # whole page down with a 500. A partial row is a degraded
            # message, not a failed request.
            for row in stored:
                if not isinstance(row, dict) or not row.get("id"):
                    continue
                messages.append(
                    ChatMessage(
                        id=str(row["id"]).split(":", 1)[-1],
                        type=str(row.get("role") or "unknown"),
                        content=str(row.get("content") or ""),
                        timestamp=(
                            str(row.get("created")) if row.get("created") else None
                        ),
                    )
                )
        if not messages and thread_state and "messages" in (thread_state.values or {}):
            messages = extract_chat_messages(thread_state.values["messages"])
            # Backfill, so this session can sync from now on.
            try:
                backfilled = await persist_chat_messages(
                    full_session_id, thread_state.values["messages"]
                )
                if backfilled:
                    logger.info(
                        f"Backfilled {backfilled} message(s) for legacy session "
                        f"{full_session_id}"
                    )
            except Exception as exc:  # noqa: BLE001 - reading must not fail
                logger.warning(f"Could not backfill {full_session_id}: {exc}")

        # Find notebook_id (we need to query the relationship)
        notebook_query = await repo_query(
            "SELECT out FROM refers_to WHERE in = $session_id",
            {"session_id": ensure_record_id(full_session_id)},
        )

        notebook_id = notebook_query[0]["out"] if notebook_query else None

        if not notebook_id:
            # This might be an old session created before API migration
            logger.warning(
                f"No notebook relationship found for session {session_id} - may be an orphaned session"
            )

        return ChatSessionWithMessagesResponse(
            id=session.id or "",
            title=session.title or "Untitled Session",
            notebook_id=notebook_id,
            created=str(session.created),
            updated=str(session.updated),
            message_count=len(messages),
            messages=messages,
            model_override=getattr(session, "model_override", None),
        )
    except NotFoundError:
        raise HTTPException(status_code=404, detail="Session not found")
    except HTTPException:
        raise
    except OpenNotebookError:
        raise
    except Exception as e:
        logger.error(f"Error fetching session: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error fetching session: {str(e)}")


@router.put("/chat/sessions/{session_id}", response_model=ChatSessionResponse)
async def update_session(session_id: str, request: UpdateSessionRequest):
    """Update session title."""
    try:
        # Get session (normalizes the ID and 404s if missing)
        full_session_id, session = await get_session_or_404(session_id)

        update_data = request.model_dump(exclude_unset=True)

        if "title" in update_data:
            session.title = update_data["title"]

        if "model_override" in update_data:
            session.model_override = update_data["model_override"]

        await session.save()

        # Find notebook_id
        notebook_query = await repo_query(
            "SELECT out FROM refers_to WHERE in = $session_id",
            {"session_id": ensure_record_id(full_session_id)},
        )
        notebook_id = notebook_query[0]["out"] if notebook_query else None

        # Get message count from LangGraph state
        msg_count = await get_session_message_count(chat_graph, full_session_id)

        return ChatSessionResponse(
            id=session.id or "",
            title=session.title or "",
            notebook_id=notebook_id,
            created=str(session.created),
            updated=str(session.updated),
            message_count=msg_count,
            model_override=session.model_override,
        )
    except NotFoundError:
        raise HTTPException(status_code=404, detail="Session not found")
    except HTTPException:
        raise
    except OpenNotebookError:
        raise
    except Exception as e:
        logger.error(f"Error updating session: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error updating session: {str(e)}")


@router.delete("/chat/sessions/{session_id}", response_model=SuccessResponse)
async def delete_session(session_id: str):
    """Delete a chat session."""
    try:
        # Get session (normalizes the ID and 404s if missing)
        _full_session_id, session = await get_session_or_404(session_id)

        await session.delete()

        return SuccessResponse(success=True, message="Session deleted successfully")
    except NotFoundError:
        raise HTTPException(status_code=404, detail="Session not found")
    except HTTPException:
        raise
    except OpenNotebookError:
        raise
    except Exception as e:
        logger.error(f"Error deleting session: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error deleting session: {str(e)}")


@router.post("/chat/execute", response_model=ExecuteChatResponse)
async def execute_chat(request: ExecuteChatRequest):
    """Execute a chat request and get AI response."""
    try:
        # Verify session exists (normalizes the ID and 404s if missing)
        full_session_id, session = await get_session_or_404(request.session_id)

        # Fetch notebook linked to this session
        notebook_query = await repo_query(
            "SELECT out FROM refers_to WHERE in = $session_id",
            {"session_id": ensure_record_id(full_session_id)},
        )
        notebook = None
        if notebook_query:
            notebook = await Notebook.get(notebook_query[0]["out"])

        # Determine model override (per-request override takes precedence over session-level)
        model_override = (
            request.model_override
            if request.model_override is not None
            else getattr(session, "model_override", None)
        )

        # Get current state
        # Use sync get_state() in a thread since SqliteSaver doesn't support async
        current_state = await asyncio.to_thread(
            chat_graph.get_state,
            config=RunnableConfig(configurable={"thread_id": full_session_id}),
        )

        # Prepare state for execution
        state_values = current_state.values if current_state else {}
        state_values["messages"] = state_values.get("messages", [])
        state_values["context"] = request.context
        state_values["notebook"] = notebook
        state_values["model_override"] = model_override

        # Add user message to state
        from langchain_core.messages import HumanMessage

        # Explicit id so a failed turn can remove it from the checkpoint.
        user_message = HumanMessage(content=request.message, id=str(uuid4()))
        state_values["messages"].append(user_message)

        # Execute chat graph in a thread so the synchronous LangGraph invoke
        # (SqliteSaver checkpoints are sync) doesn't block the event loop and
        # freeze the rest of the API while the LLM responds. Mirrors the
        # get_state() calls above.
        # invoke_chat_turn also drops the question from the checkpoint when the
        # turn fails, so a retry doesn't add it twice.
        result = await asyncio.to_thread(
            invoke_chat_turn,
            chat_graph,
            state_values,
            RunnableConfig(
                configurable={
                    "thread_id": full_session_id,
                    "model_id": model_override,
                }
            ),
            user_message,
        )

        # Update session timestamp
        await session.save()

        # Make the transcript durable. The checkpoint keeps working as a
        # resume cache; this is the copy that can be merged, and so the
        # copy that can cross to the other device. Idempotent, so a
        # conversation that predates this table backfills on first use.
        turn_messages = result.get("messages", [])
        try:
            added = await persist_chat_messages(full_session_id, turn_messages)
            if added:
                logger.info(f"Persisted {added} chat message(s) for {full_session_id}")
        except Exception as exc:  # noqa: BLE001 - the turn already succeeded
            # Never fail a completed turn over bookkeeping. The checkpoint
            # still holds the conversation, so the worst case is that it
            # does not sync until the migration has run.
            logger.warning(
                f"Could not persist chat messages for {full_session_id}: "
                f"{type(exc).__name__}: {exc}"
            )

        # Convert messages to response format
        messages = extract_chat_messages(turn_messages)

        return ExecuteChatResponse(session_id=request.session_id, messages=messages)
    except NotFoundError:
        raise HTTPException(status_code=404, detail="Session not found")
    except HTTPException:
        raise
    except OpenNotebookError:
        raise
    except Exception as e:
        # Log detailed error with context for debugging
        logger.error(
            f"Error executing chat: {str(e)}\n"
            f"  Session ID: {request.session_id}\n"
            f"  Model override: {request.model_override}\n"
            f"  Traceback:\n{traceback.format_exc()}"
        )
        raise HTTPException(status_code=500, detail=f"Error executing chat: {str(e)}")


def _eligible_ids(
    context_config: Dict[str, Any],
) -> Optional[Dict[str, set]]:
    """Which records the user's inclusion config makes eligible for retrieval.

    "not in" is the exclusion the UI writes for an unselected row, so it is
    the only status that disqualifies something. Returns None when there is
    no config, meaning the whole notebook is in scope.
    """
    if not context_config:
        return None
    return {
        "sources": {
            _ensure_prefix("source", str(sid))
            for sid, status in (context_config.get("sources") or {}).items()
            if "not in" not in str(status)
        },
        "notes": {
            _ensure_prefix("note", str(nid))
            for nid, status in (context_config.get("notes") or {}).items()
            if "not in" not in str(status)
        },
    }


@router.post("/chat/context", response_model=BuildContextResponse)
async def build_context(request: BuildContextRequest):
    """Build context for a notebook based on context configuration."""
    try:
        # Verify notebook exists
        notebook = await Notebook.get(request.notebook_id)
        if not notebook:
            raise HTTPException(status_code=404, detail="Notebook not found")

        retrieval_report = None
        context_data = None
        total_content = ""

        if request.query and request.query.strip():
            eligible = _eligible_ids(request.context_config)
            try:
                (
                    context_data,
                    total_content,
                    retrieval_report,
                ) = await build_relevant_context(
                    notebook_id=request.notebook_id,
                    query=request.query.strip(),
                    eligible_ids=eligible,
                    limit=request.retrieval_limit,
                )
            except Exception as exc:  # noqa: BLE001 - fall back, never fail
                logger.info(f"Falling back to whole-document context: {exc}")
                if retrieval_report is None:
                    retrieval_report = {"mode": "fallback", "reason": str(exc)[:160]}

            # Nothing matched above the similarity bar. An empty prompt is
            # worse than the document dump, so fall back rather than send
            # the model nothing.
            if retrieval_report and retrieval_report.get("mode") == "empty":
                context_data = None

        if context_data is None:
            context_data, total_content = await build_notebook_context(
                notebook, request.context_config
            )
            if retrieval_report is not None:
                retrieval_report["mode"] = (
                    "fallback"
                    if retrieval_report.get("mode") == "empty"
                    else "documents"
                )

        char_count = len(total_content)
        estimated_tokens = token_count(total_content) if total_content else 0

        return BuildContextResponse(
            context=context_data,
            token_count=estimated_tokens,
            char_count=char_count,
            retrieval=retrieval_report,
        )
    except HTTPException:
        raise
    except OpenNotebookError:
        raise
    except Exception as e:
        logger.error(f"Error building context: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error building context: {str(e)}")
