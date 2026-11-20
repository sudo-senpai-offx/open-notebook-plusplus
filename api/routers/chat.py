import asyncio
import os
import traceback
from pathlib import Path
from typing import Any, Dict, List, Optional
from uuid import uuid4

# ---------------------------------------------------------------------------
# Internet connectivity probe (cached, 30s TTL) for cloud↔local switching.
# Probes chat.deepseek.com, google.com (AI Mode), and duck.ai. Internet is
# "available" when >= 2 of 3 respond within the timeout. Results are cached
# for 30 seconds so the probe doesn't add latency to every turn.
# ---------------------------------------------------------------------------
import httpx
from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile
from langchain_core.runnables import RunnableConfig
from loguru import logger
from pydantic import BaseModel, Field

from api.routers._chat_shared import (
    ChatMessage,
    SuccessResponse,
    extract_chat_messages,
    get_session_or_404,
    transcript_sync_state,
)
from open_notebook.ai.models import ProcessingFlags
from open_notebook.ai.provider_registry import get_provider_registry
from open_notebook.ai.providers import PROVIDER_DEEPSEEK_WEB
from open_notebook.services.deepseek_direct_service import (
    deepseek_direct_service,
)
from open_notebook.database.repository import ensure_record_id, repo_query
from open_notebook.domain.attachment import Attachment
from open_notebook.domain.notebook import (
    ChatSession,
    Notebook,
    persist_chat_messages,
    update_chat_message,
)
from open_notebook.exceptions import (
    NotFoundError,
    OpenNotebookError,
)
from open_notebook.graphs.chat import graph as chat_graph
from open_notebook.services.prompter_service import prompter_service
from open_notebook.services.quota_tracker import quota_tracker
from open_notebook.utils import token_count
from open_notebook.utils.context_builder import (
    _ensure_prefix,
    build_notebook_context,
    build_relevant_context,
)

_internet_probe_cache: dict[str, Any] = {"result": None, "ts": 0.0}
_PROBE_CACHE_TTL = 30.0
_PROBE_TARGETS = [
    ("deepseek-web", "https://chat.deepseek.com", 5.0),
    ("google-ai-mode", "https://www.google.com", 5.0),
    ("duck-ai", "https://duck.ai", 5.0),
]


async def _probe_internet() -> bool:
    """Return True when >= 2 of 3 cloud endpoints are reachable.

    Cached for _PROBE_CACHE_TTL seconds. Never raises on failure.
    """
    now = asyncio.get_event_loop().time()
    if (
        _internet_probe_cache["result"] is not None
        and now - _internet_probe_cache["ts"] < _PROBE_CACHE_TTL
    ):
        return _internet_probe_cache["result"]

    ok_count = 0
    async with httpx.AsyncClient() as client:
        for _name, url, timeout in _PROBE_TARGETS:
            try:
                resp = await client.head(url, timeout=timeout, follow_redirects=True)
                if resp.status_code < 500:
                    ok_count += 1
            except Exception:  # noqa: BLE001
                continue

    result = ok_count >= 2
    _internet_probe_cache["result"] = result
    _internet_probe_cache["ts"] = now
    return result


def _resolve_provider_and_model(
    model_override: Optional[str],
) -> tuple[Optional[str], Optional[str]]:
    """Resolve provider_id and model_name from a model_override string.

    Accepts:
    - None -> (None, None)  [use defaults]
    - "deepseek-web" -> ("deepseek-web", None)  [provider only, default model]
    - "duck-ai:gpt-6-luna" -> ("duck-ai", "gpt-6-luna")  [provider:model]
    - "model:deepseek-web" -> ("deepseek-web", None)  [legacy SurrealDB record ID format]
    """
    if not model_override:
        return None, None

    # Strip SurrealDB record ID prefix if present.
    value = model_override
    if value.startswith("model:"):
        value = value[len("model:"):]

    if ":" in value:
        provider_id, model_name = value.split(":", 1)
        return provider_id, model_name or None

    # Treat as provider ID only.
    return value, None


def _get_default_provider() -> str:
    """Return the default cloud provider (deepseek-web)."""
    return PROVIDER_DEEPSEEK_WEB


async def _send_via_provider(
    provider_id: str,
    model_name: Optional[str],
    message: str,
    *,
    session_id: Optional[str] = None,
    thinking_enabled: bool = False,
    ref_file_ids: Optional[List[str]] = None,
    timeout: float = 120.0,
) -> Any:
    """Send a message through the specified provider using the ProviderRegistry.

    Returns a ProviderSendResult.
    """
    registry = get_provider_registry()
    provider = registry.get(provider_id)
    if provider is None:
        from open_notebook.ai.providers import ProviderSendResult

        return ProviderSendResult(
            ok=False,
            error=f"Provider '{provider_id}' is not registered.",
            used_provider=provider_id,
        )

    if not await provider.is_available():
        from open_notebook.ai.providers import ProviderSendResult

        return ProviderSendResult(
            ok=False,
            error=(
                f"Provider '{provider_id}' is not available. "
                "Check credentials and configuration in Settings."
            ),
            used_provider=provider_id,
        )

    return await provider.send_message(
        message=message,
        session_id=session_id,
        thinking_enabled=thinking_enabled,
        ref_file_ids=ref_file_ids,
        timeout=timeout,
        model=model_name,
    )


from open_notebook.utils.graph_utils import (
    get_session_message_count,
    invoke_chat_turn,
)
from open_notebook.utils.history import (
    DEFAULT_CONTEXT_TOKENS,
    parse_since,
    select_history,
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
    # Sync state, computed at read time from the blob store and the last
    # export - no schema change, no stored state to go stale. `stored`
    # means this device holds the transcript bytes for the current
    # messages; `exported` means the manifest names exactly those bytes.
    # Neither claims the other device has them - that is unknowable
    # here - only whether there is anything left to send.
    transcript_stored: bool = Field(
        default=False, description="Transcript bytes held on this device"
    )
    transcript_exported: bool = Field(
        default=False, description="Manifest names the current transcript hash"
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
    # Narrow the conversation the model sees: an ISO timestamp, or a
    # shorthand like "2d" / "36h". Without it, history is filled from the
    # newest end until the context budget is spent.
    history_since: Optional[str] = Field(
        default=None,
        description="Only consider history at or after this time (ISO, or 45m/2d/1w)",
    )
    attachments: Optional[List[str]] = Field(
        None,
        description="List of local file paths to attach to the message",
    )


class HistorySelectionReport(BaseModel):
    """What the model actually saw, for the "using the last N" indicator.

    A conversation that silently stops remembering is worse than one
    that says so. These numbers let the UI state what was sent rather
    than implying the whole history was.
    """

    kept_exchanges: int = Field(..., description="Exchanges sent to the model")
    dropped_exchanges: int = Field(..., description="Exchanges trimmed away")
    total_exchanges: int = Field(..., description="Exchanges in the session")
    reason: str = Field(..., description="Why trimming happened: budget or timeframe")


class ExecuteChatResponse(BaseModel):
    session_id: str = Field(..., description="Session ID")
    messages: List[ChatMessage] = Field(..., description="Updated message list")
    history_selection: Optional[HistorySelectionReport] = Field(
        default=None,
        description="Which exchanges the model saw, when trimming applied",
    )
    cloud_available: bool = Field(
        default=False,
        description="Whether cloud providers are reachable (internet probe)",
    )
    switch_to_cloud_suggested: bool = Field(
        default=False,
        description="UI hint: cloud is available while using local model, "
        "offer user a switch",
    )


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
            stored, exported = await transcript_sync_state(session_id)

            results.append(
                ChatSessionResponse(
                    id=session.id or "",
                    title=session.title or "Untitled Session",
                    notebook_id=notebook_id,
                    created=str(session.created) if session.created else "",
                    updated=str(session.updated) if session.updated else "",
                    message_count=msg_count,
                    model_override=getattr(session, "model_override", None),
                    transcript_stored=stored,
                    transcript_exported=exported,
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
            created=str(session.created) if session.created else "",
            updated=str(session.updated) if session.updated else "",
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
            created=str(session.created) if session.created else "",
            updated=str(session.updated) if session.updated else "",
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
            created=str(session.created) if session.created else "",
            updated=str(session.updated) if session.updated else "",
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


class ChatAttachmentUploadResponse(BaseModel):
    file_path: str = Field(..., description="Local path of the uploaded file")
    filename: str = Field(..., description="Original filename")


@router.post("/chat/attachments", response_model=ChatAttachmentUploadResponse)
async def upload_chat_attachment(
    file: UploadFile = File(...),
    upload_session_id: Optional[str] = Form(
        None,
        description="ON++ session id that uploaded this attachment (source traceability)",
    ),
) -> ChatAttachmentUploadResponse:
    """Upload a file for use as a chat attachment.

    The file is saved to the local attachments directory so the backend can
    reference it when sending messages to models that support file uploads
    (e.g. deepseek-web). Returns the local path to use in the
    ``attachments`` field of ``POST /chat/execute``.
    """
    try:
        upload_dir = Path(
            os.getenv("OPEN_NOTEBOOK_ATTACHMENTS_DIR", "./data/attachments")
        )
        upload_dir.mkdir(parents=True, exist_ok=True)

        # Sanitize filename and make it unique
        orig_name = file.filename or "file"
        safe_name = "".join(c for c in orig_name if c.isalnum() or c in "._-") or "file"
        dest = upload_dir / f"{uuid4().hex[:8]}_{safe_name}"

        contents = await file.read()
        dest.write_bytes(bytes(contents))

        # Create an Attachment record so it can be listed/read/renamed/deleted
        # independently of any notebook (separate table from notebooks).
        try:
            mime_type = file.content_type or "application/octet-stream"
            attachment = Attachment(
                file_path=str(dest),
                filename=file.filename or safe_name,
                mime_type=mime_type,
                file_size=len(contents),
                upload_session_id=upload_session_id,
                is_orphaned=False,
            )
            await attachment.save()
            logger.info(
                f"Attachment record created: {attachment.id} -> {dest} "
                f"(session={upload_session_id})"
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"File saved but Attachment record creation failed: {exc}")

        return ChatAttachmentUploadResponse(
            file_path=str(dest),
            filename=file.filename or safe_name,
        )
    except Exception as e:  # noqa: BLE001
        logger.error(f"Chat attachment upload failed: {e}")
        raise HTTPException(
            status_code=500, detail=f"Attachment upload failed: {str(e)}"
        )


class AttachmentListResponse(BaseModel):
    attachments: list[dict] = Field(
        ...,
        description="List of attachment records (id, filename, mime_type, file_size, upload_session_id, is_orphaned, created)",
    )


class AttachmentContentResponse(BaseModel):
    file_path: str = Field(..., description="Local path of the attachment file")
    filename: str = Field(..., description="Original filename")
    mime_type: str = Field(..., description="MIME type")
    content: str = Field(
        ..., description="Text content of the attachment (for text files)"
    )


@router.get("/chat/attachments", response_model=AttachmentListResponse)
async def list_chat_attachments() -> AttachmentListResponse:
    """List all attachment records (separate from notebooks).

    Returns metadata for every uploaded attachment so the frontend can
    display them in a reader overlay and offer delete.
    """
    try:
        attachments = await Attachment.list_all()
        return AttachmentListResponse(
            attachments=[
                {
                    "id": a.id,
                    "file_path": a.file_path,
                    "filename": a.filename,
                    "mime_type": a.mime_type,
                    "file_size": a.file_size,
                    "upload_session_id": a.upload_session_id,
                    "is_orphaned": a.is_orphaned,
                    "created": a.created.isoformat() if a.created else None,
                }
                for a in attachments
            ]
        )
    except Exception as e:  # noqa: BLE001
        logger.error(f"Error listing attachments: {e}")
        raise HTTPException(
            status_code=500, detail=f"Failed to list attachments: {str(e)}"
        )


@router.get(
    "/chat/attachments/{attachment_id}/content",
    response_model=AttachmentContentResponse,
)
async def get_attachment_content(attachment_id: str) -> AttachmentContentResponse:
    """Read the text content of an attachment for the reader overlay.

    Only text-based files are supported. Binary files return an empty
    content string with a note in the frontend.
    """
    try:
        attachment = await Attachment.get_by_id(attachment_id)
        if attachment is None:
            raise HTTPException(status_code=404, detail="Attachment not found")

        content = ""
        path = Path(attachment.file_path)
        if path.exists() and path.is_file():
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
                replacement_ratio = text.count("\ufffd") / max(len(text), 1)
                if replacement_ratio > 0.30:
                    # Binary file — skip text prepend, content stays empty
                    logger.warning(
                        f"Attachment appears binary, content not readable: {attachment.filename}"
                    )
                else:
                    content = text
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"Could not read attachment {attachment_id}: {exc}")

        return AttachmentContentResponse(
            file_path=str(path),
            filename=attachment.filename,
            mime_type=attachment.mime_type,
            content=content,
        )
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        logger.error(f"Error reading attachment content: {e}")
        raise HTTPException(
            status_code=500, detail=f"Failed to read attachment: {str(e)}"
        )


@router.delete("/chat/attachments/{attachment_id}")
async def delete_chat_attachment(attachment_id: str) -> dict:
    """Delete an attachment record and attempt to unlink the underlying file."""
    try:
        attachment = await Attachment.get_by_id(attachment_id)
        if attachment is None:
            raise HTTPException(status_code=404, detail="Attachment not found")

        await attachment.delete()
        return {"ok": True, "message": f"Attachment '{attachment.filename}' deleted"}
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        logger.error(f"Error deleting attachment: {e}")
        raise HTTPException(
            status_code=500, detail=f"Failed to delete attachment: {str(e)}"
        )


class AttachmentRenameRequest(BaseModel):
    new_filename: str = Field(
        ..., description="New display filename for the attachment"
    )


@router.patch("/chat/attachments/{attachment_id}/rename")
async def rename_chat_attachment(
    attachment_id: str,
    request: AttachmentRenameRequest,
) -> dict:
    """Rename an attachment's display filename.

    The underlying file on disk is NOT renamed — only the record's
    display name changes.
    """
    try:
        attachment = await Attachment.get_by_id(attachment_id)
        if attachment is None:
            raise HTTPException(status_code=404, detail="Attachment not found")

        await attachment.rename(request.new_filename)
        return {
            "ok": True,
            "message": f"Attachment renamed to '{request.new_filename}'",
            "filename": attachment.filename,
        }
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        logger.error(f"Error renaming attachment: {e}")
        raise HTTPException(
            status_code=500, detail=f"Failed to rename attachment: {str(e)}"
        )


def _context_tokens() -> int:
    """The model's window, as told to us by the launcher.

    The phone runs `llama-server -c 4096`; termux-launch.sh exports the
    same number so the two cannot drift. Anything not set falls back to
    the observed device value rather than a generous default - assuming
    a big window is what makes a phone-sized one fail.
    """
    raw = os.getenv("OPEN_NOTEBOOK_CHAT_CONTEXT", "").strip()
    try:
        value = int(raw) if raw else DEFAULT_CONTEXT_TOKENS
    except ValueError:
        return DEFAULT_CONTEXT_TOKENS
    return value if value >= 512 else DEFAULT_CONTEXT_TOKENS


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

        # Probe internet connectivity for cloud↔local switching decisions.
        internet_available = await _probe_internet()

        # Get current state
        # Use sync get_state() in a thread since SqliteSaver doesn't support async
        current_state = await asyncio.to_thread(
            chat_graph.get_state,
            config=RunnableConfig(configurable={"thread_id": full_session_id}),
        )

        # Prepare state for execution
        state_values = current_state.values if current_state else {}

        # Trim the history to what the model can actually read. Without
        # this the entire conversation is sent every turn, and a
        # long-running session silently stops working once it outgrows
        # the window - no error, just a server that truncates or refuses.
        history = select_history(
            current_state.values.get("messages", []) if current_state else [],
            context_tokens=_context_tokens(),
            history_since=parse_since(request.history_since),
        )
        if history.dropped_exchanges:
            logger.info(
                f"History trimmed for {full_session_id}: kept "
                f"{history.kept_exchanges} exchange(s), dropped "
                f"{history.dropped_exchanges} ({history.reason}), "
                f"{history.used_tokens}/{history.budget_tokens} tokens"
            )
        state_values["messages"] = list(history.messages)
        state_values["context"] = request.context
        state_values["notebook"] = notebook
        state_values["model_override"] = model_override

        # Resolve provider for attachment upload
        provider_id_for_attach, _ = _resolve_provider_and_model(model_override)
        if not provider_id_for_attach:
            provider_id_for_attach = _get_default_provider()
        from open_notebook.ai.provider_registry import get_provider_registry
        attach_provider = get_provider_registry().get(provider_id_for_attach)

        # Add user message to state
        from langchain_core.messages import HumanMessage

        # Prepend attachment contents to the message if any
        message_content = request.message
        ref_file_ids: list[str] = []
        failed_attachments: list[str] = []
        if request.attachments:
            attachment_parts: list[str] = []
            for file_path in request.attachments:
                try:
                    path = Path(file_path)
                    if path.exists() and path.is_file():
                        # Upload to deepseek-web so the model can reference
                        # the file directly (covers binary files too).
                        try:
                            file_id = attach_provider.upload_attachment(file_path)
                            if file_id:
                                ref_file_ids.append(file_id)
                                logger.info(
                                    f"Attachment {path.name} uploaded to "
                                    f"deepseek-web as {file_id}"
                                )
                            else:
                                failed_attachments.append(path.name)
                                logger.warning(
                                    f"Attachment {path.name}: deepseek-web "
                                    f"file upload returned no file ID"
                                )
                        except Exception as exc:
                            failed_attachments.append(path.name)
                            logger.warning(
                                f"Could not upload attachment to deepseek-web: "
                                f"{file_path} — {exc}"
                            )

                        # Read text content for message prepend (text files only).
                        try:
                            content = path.read_text(encoding="utf-8", errors="replace")
                            replacement_ratio = content.count("\ufffd") / max(
                                len(content), 1
                            )
                            if replacement_ratio > 0.30:
                                logger.warning(
                                    f"Attachment appears binary, skipping text prepend: {path.name}"
                                )
                            else:
                                attachment_parts.append(
                                    f"\n--- Attachment: {path.name} ---\n{content}\n--- End of {path.name} ---"
                                )
                        except Exception as exc:  # noqa: BLE001
                            logger.warning(
                                f"Could not read attachment {file_path}: {exc}"
                            )
                    else:
                        logger.warning(f"Attachment not found: {file_path}")
                        failed_attachments.append(Path(file_path).name)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(f"Could not process attachment {file_path}: {exc}")
                    failed_attachments.append(Path(file_path).name)
            if attachment_parts:
                message_content = (
                    request.message + "\n\n" + "".join(attachment_parts) + "\n\n---\n"
                )

            if attachment_parts:
                message_content = (
                    request.message + "\n\n" + "".join(attachment_parts) + "\n\n---\n"
                )

            # If model_override is deepseek-web and attachments were provided
            # but none were successfully uploaded AND none could be read locally,
            # fail loudly so the user knows.
            if (
                model_override == "deepseek-web"
                and failed_attachments
                and not attachment_parts
            ):
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "Failed to upload or read the following attachment(s): "
                        f"{', '.join(failed_attachments)}. "
                        "Ensure DeepSeek-web credentials are configured in "
                        "Settings and the PoW solver is installed "
                        "(pip install deepseek-pow wasmtime)."
                    ),
                )

        # Explicit id so a failed turn can remove it from the checkpoint.
        user_message = HumanMessage(content=message_content, id=str(uuid4()))
        state_values["messages"].append(user_message)

        # --- Provider-based routing (DeepSeek-web, Google AI Mode, Duck.ai) ---
        # All three cloud providers are equal priority; default is deepseek-web.
        # model_override can be "provider_id" or "provider_id:model_name".
        provider_id, model_name = _resolve_provider_and_model(model_override)
        if not provider_id:
            provider_id = _get_default_provider()

        # Determine the provider config for attachment limits.
        from open_notebook.ai.providers import get_provider_config, ProviderSendResult

        provider_cfg = get_provider_config(provider_id)

        # Validate attachment limits for the selected provider.
        if request.attachments and provider_cfg:
            if len(request.attachments) > provider_cfg.max_files:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Too many attachments for {provider_cfg.display_name}: "
                        f"max {provider_cfg.max_files} files per chat session."
                    ),
                )
            for file_path in request.attachments:
                path = Path(file_path)
                if path.exists() and path.is_file():
                    size_mb = path.stat().st_size / (1024 * 1024)
                    if size_mb > provider_cfg.max_size_mb:
                        raise HTTPException(
                            status_code=400,
                            detail=(
                                f"Attachment {path.name} ({size_mb:.1f} MB) exceeds "
                                f"{provider_cfg.display_name} max size "
                                f"({provider_cfg.max_size_mb} MB)."
                            ),
                        )

        # Upload attachments via the provider (if supported).
        provider_ref_file_ids: List[str] = []
        if request.attachments and provider_cfg and provider_cfg.supports_attachments:
            for file_path in request.attachments:
                try:
                    path = Path(file_path)
                    if path.exists() and path.is_file():
                        # Upload to the provider so the model can reference
                        # the file directly (covers binary files too).
                        file_id = await provider.upload_attachment(file_path)
                        if file_id:
                            provider_ref_file_ids.append(file_id)
                            logger.info(
                                f"Attachment {path.name} uploaded to "
                                f"{provider_id} as {file_id}"
                            )
                        else:
                            failed_attachments.append(path.name)
                            logger.warning(
                                f"Attachment {path.name}: {provider_id} "
                                f"file upload returned no file ID"
                            )
                    else:
                        failed_attachments.append(Path(file_path).name)
                        logger.warning(f"Attachment not found: {file_path}")
                except Exception as exc:  # noqa: BLE001
                    failed_attachments.append(Path(file_path).name)
                    logger.warning(
                        f"Could not process attachment {file_path}: {exc}"
                    )

        # If attachments were provided but none were successfully uploaded
        # AND none could be read locally, fail loudly.
        if (
            request.attachments
            and failed_attachments
            and not attachment_parts
            and not provider_ref_file_ids
        ):
            raise HTTPException(
                status_code=400,
                detail=(
                    "Failed to upload or read the following attachment(s): "
                    f"{', '.join(failed_attachments)}. "
                    f"Ensure {provider_cfg.display_name} credentials are "
                    f"configured in Settings."
                ),
            )

        # Build the message content with attachment text prepended.
        if attachment_parts:
            message_content = (
                request.message + "\n\n" + "".join(attachment_parts) + "\n\n---\n"
            )
        else:
            message_content = request.message

        # Explicit id so a failed turn can remove it from the checkpoint.
        user_message = HumanMessage(content=message_content, id=str(uuid4()))
        state_values["messages"].append(user_message)

        # Transform message through prompter (session-aware).
        transformed_prompt = await prompter_service.transform(
            raw_message=message_content,
            session_id=request.session_id,
            notebook_id=notebook.id if notebook else None,
            message_type="chat",
        )

        # Internet-aware send with retry: when offline, retry up to 10
        # times with exponential backoff (5s doubling, cap 60s) before
        # asking the user to fall back to a local model.
        send_result = None
        last_error: Optional[str] = None
        backoff = 5.0
        for attempt in range(1, 11):
            if not internet_available:
                last_error = (
                    f"No internet connectivity (attempt {attempt}/10). "
                    f"Retrying in {min(backoff, 60.0)}s..."
                )
                logger.warning(last_error)
                await asyncio.sleep(min(backoff, 60.0))
                backoff = min(backoff * 2, 60.0)
                internet_available = await _probe_internet()
                continue
            try:
                send_result = await _send_via_provider(
                    provider_id=provider_id,
                    model_name=model_name,
                    message=transformed_prompt,
                    session_id=request.session_id,
                    thinking_enabled=True,
                    ref_file_ids=provider_ref_file_ids or None,
                    timeout=120.0,
                )
                break
            except Exception as exc:  # noqa: BLE001
                msg = str(exc)
                last_error = msg
                logger.warning(
                    f"Provider '{provider_id}' send failed (attempt {attempt}/10): {msg}"
                )
                await asyncio.sleep(min(backoff, 60.0))
                backoff = min(backoff * 2, 60.0)

        if send_result is None or not send_result.ok:
            # Determine the appropriate error response.
            if send_result and send_result.manual_login_required:
                raise HTTPException(
                    status_code=403,
                    detail=(
                        f"{provider_cfg.display_name if provider_cfg else provider_id} "
                        f"session expired or invalid credentials. "
                        f"Re-log in and update credentials in Settings."
                    ),
                )
            if send_result and send_result.rate_limited:
                raise HTTPException(
                    status_code=429,
                    detail=(
                        f"{provider_cfg.display_name if provider_cfg else provider_id} "
                        f"rate limited. Wait for quota reset or enable 'Allow local "
                        f"processing' in Settings to use local models."
                    ),
                )
            raise HTTPException(
                status_code=503,
                detail=(
                    f"{provider_cfg.display_name if provider_cfg else provider_id} "
                    f"unavailable after 10 retries: {last_error or 'unknown error'}. "
                    f"Enable 'Allow local processing' in Settings to use "
                    f"local models offline, or check your internet connection."
                ),
            )

        response_text = send_result.response or ""
        thinking_text = send_result.thinking
        if not response_text and not thinking_text:
            raise HTTPException(
                status_code=502,
                detail=f"Empty response from {provider_cfg.display_name if provider_cfg else provider_id}.",
            )

        # Increment quota for this provider.
        quota_provider = provider_id if provider_id != "ollama" else "local"
        await quota_tracker.increment(quota_provider, 1)

        # Link the ON++ chat session to its provider session series.
        try:
            nb_query = await repo_query(
                "SELECT out FROM refers_to WHERE in = $sid",
                {"sid": ensure_record_id(full_session_id)},
            )
            if nb_query:
                session.provider_session_id = send_result.session_id or ""
                await session.save()
        except Exception as exc:  # noqa: BLE001 - never fail a turn over this
            logger.warning(
                f"Could not link chat session to provider series: {exc}"
            )

        # Build assistant message
        from langchain_core.messages import AIMessage

        assistant_message = AIMessage(
            content=response_text or "",
            id=str(uuid4()),
            additional_kwargs={"thinking": thinking_text} if thinking_text else {},
            additional_kwargs2={
                "provider": provider_id,
                "model": model_name or (provider_cfg.default_model if provider_cfg else None),
            } if (provider_id or model_name) else {},
        )
        state_values["messages"].append(assistant_message)

        # Persist the turn
        await session.save()
        turn_messages = [user_message, assistant_message]
        try:
            added = await persist_chat_messages(full_session_id, turn_messages)
            if added:
                logger.info(
                    f"Persisted {added} chat message(s) for {full_session_id}"
                )
        except Exception as exc:
            logger.warning(
                f"Could not persist chat messages for {full_session_id}: "
                f"{type(exc).__name__}: {exc}"
            )

        messages = extract_chat_messages(turn_messages)

        return ExecuteChatResponse(
            session_id=request.session_id,
            messages=messages,
            history_selection=HistorySelectionReport(
                kept_exchanges=1,
                dropped_exchanges=0,
                total_exchanges=1,
                reason=f"{provider_id} response",
            ),
            cloud_available=internet_available,
            switch_to_cloud_suggested=False,
        )

        # Execute chat graph in a thread so the synchronous LangGraph invoke
        # (SqliteSaver checkpoints are sync) doesn't block the event loop and
        # freeze the rest of the API while the LLM responds. Mirrors the
        # get_state() calls above.
        # invoke_chat_turn also drops the question from the checkpoint when the
        # turn fails, so a retry doesn't add it twice.
        #
        # Quota check for non-deepseek-web cloud providers.
        # model_override may now be "provider_id" or "provider_id:model_name".
        provider_id_for_quota, _ = _resolve_provider_and_model(model_override)
        if not provider_id_for_quota:
            provider_id_for_quota = _get_default_provider()
        is_cloud_model = provider_id_for_quota != "ollama"
        if is_cloud_model:
            if not await quota_tracker.can_request(provider_id_for_quota):
                flags = await ProcessingFlags.get_instance()
                if flags.allow_local_processing:
                    model_override = "ollama"
                    provider_id_for_quota = "ollama"
                    logger.info(
                        f"Quota exceeded for {provider_id_for_quota}; switching to local llama"
                    )
                else:
                    raise HTTPException(
                        status_code=429,
                        detail=(
                            f"Cloud quota exceeded for {provider_id_for_quota}. "
                            f"Enable 'Allow local processing' in Settings to use "
                            f"local models, or wait for quota reset."
                        ),
                    )

        # Suggest switching to cloud when on a local model and internet is back.
        is_local_model = provider_id_for_quota == "ollama"
        switch_to_cloud = internet_available and is_local_model

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

        return ExecuteChatResponse(
            session_id=request.session_id,
            messages=messages,
            history_selection=HistorySelectionReport(
                kept_exchanges=history.kept_exchanges,
                dropped_exchanges=history.dropped_exchanges,
                total_exchanges=history.kept_exchanges + history.dropped_exchanges,
                reason=history.reason,
            ),
            cloud_available=internet_available,
            switch_to_cloud_suggested=switch_to_cloud,
        )
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


class EditMessageRequest(BaseModel):
    message_id: str = Field(..., description="ID of the message to edit")
    new_content: str = Field(..., description="New message content")
    new_attachments: Optional[List[str]] = Field(
        None, description="New attachment file paths (replaces existing)"
    )


class EditMessageResponse(BaseModel):
    session_id: str
    messages: List[ChatMessage]
    edit_count: int
    continuations_deleted: int


@router.post(
    "/chat/sessions/{session_id}/messages/{message_id}/edit",
    response_model=EditMessageResponse,
)
async def edit_chat_message(
    session_id: str,
    message_id: str,
    request: EditMessageRequest,
) -> EditMessageResponse:
    """Edit a previously sent message and resend it through the model.

    When a message is edited, all subsequent messages (continuations) are
    deleted, the edited message is updated with an incremented edit counter,
    and the message is re-sent through the model. Max 4 edits per message.

    This implements the "edit and resend" flow: editing message #N deletes
    messages #N+1..#end (they were continuations of the original), then
    re-generates the response from the edited prompt.
    """
    try:
        # Verify session exists
        full_session_id, session = await get_session_or_404(session_id)

        # Fetch notebook linked to this session
        notebook_query = await repo_query(
            "SELECT out FROM refers_to WHERE in = $session_id",
            {"session_id": ensure_record_id(full_session_id)},
        )
        notebook = None
        if notebook_query:
            notebook = await Notebook.get(notebook_query[0]["out"])

        # Determine model override (session-level only; edits reuse the
        # session's configured model, there is no per-request override)
        model_override = getattr(session, "model_override", None)

        # Get all messages from the durable transcript
        rows = await repo_query(
            """
            SELECT id, seq, role, content, edit_count, created
            FROM chat_message
            WHERE session = $sid
            ORDER BY seq ASC
            """,
            {"sid": ensure_record_id(full_session_id)},
        )
        if not rows:
            raise HTTPException(status_code=404, detail="No messages in session")

        messages = [
            {
                "id": str(row["id"]).replace("chat_message:", ""),
                "seq": row["seq"],
                "role": row["role"],
                "content": row["content"] or "",
                "edit_count": row.get("edit_count") or 0,
                "created": str(row["created"]) if row.get("created") else None,
            }
            for row in rows
        ]

        # Find the message to edit
        target_idx = None
        for i, m in enumerate(messages):
            if m["id"] == message_id:
                target_idx = i
                break
        if target_idx is None:
            raise HTTPException(status_code=404, detail="Message not found")

        target_msg = messages[target_idx]

        # Validate: must be a human message
        if target_msg["role"] != "human":
            raise HTTPException(
                status_code=400,
                detail="Only human messages can be edited",
            )

        # Validate: edit count limit (max 4)
        current_edit_count = target_msg["edit_count"] or 0
        if current_edit_count >= 4:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Message has reached the maximum of 4 edits "
                    f"(current: {current_edit_count})"
                ),
            )

        # Count continuations to delete (all messages after target)
        continuations_deleted = len(messages) - target_idx - 1

        # Delete all messages after the edited one (they were continuations)
        if continuations_deleted > 0:
            await repo_query(
                """
                DELETE FROM chat_message
                WHERE session = $sid AND seq > $seq
                """,
                {
                    "sid": ensure_record_id(full_session_id),
                    "seq": target_msg["seq"],
                },
            )

        # Build the new message content with attachment prepend
        message_content = request.new_content
        ref_file_ids: list[str] = []
        attachment_parts: list[str] = []

        # Resolve provider for attachment upload
        attach_provider_id, _ = _resolve_provider_and_model(model_override)
        if not attach_provider_id:
            attach_provider_id = _get_default_provider()
        from open_notebook.ai.provider_registry import get_provider_registry
        attach_provider = get_provider_registry().get(attach_provider_id)

        if request.new_attachments:
            for file_path in request.new_attachments:
                try:
                    path = Path(file_path)
                    if path.exists() and path.is_file():
                        # Upload to deepseek-web for ref_file_ids
                        try:
                            file_id = attach_provider.upload_attachment(file_path)
                            if file_id:
                                ref_file_ids.append(file_id)
                        except Exception:
                            pass

                        # Read text content for prepend
                        try:
                            content = path.read_text(encoding="utf-8", errors="replace")
                            replacement_ratio = content.count("\ufffd") / max(
                                len(content), 1
                            )
                            if replacement_ratio <= 0.30:
                                attachment_parts.append(
                                    f"\n--- Attachment: {path.name} ---\n{content}\n--- End of {path.name} ---"
                                )
                        except Exception:
                            pass
                except Exception:
                    pass

            if attachment_parts:
                message_content = (
                    request.new_content
                    + "\n\n"
                    + "".join(attachment_parts)
                    + "\n\n---\n"
                )

        # Update the edited message in SurrealDB
        new_edit_count = current_edit_count + 1
        updated = await update_chat_message(
            full_session_id,
            message_id,
            message_content,
            new_edit_count,
        )
        if not updated:
            raise HTTPException(status_code=500, detail="Failed to update message")

        # Build context from notebook sources (same as execute)
        context: dict[str, Any] = {}
        if notebook:
            # Use build_context logic inline for simplicity
            context = {"sources": [], "notes": []}

        # --- Model invocation via Provider registry ---
        provider_id, model_name = _resolve_provider_and_model(model_override)
        if not provider_id:
            provider_id = _get_default_provider()

        from open_notebook.ai.providers import get_provider_config, ProviderSendResult

        provider_cfg = get_provider_config(provider_id)

        # Quota check for the selected provider.
        quota_provider = provider_id if provider_id != "ollama" else "local"
        if not await quota_tracker.can_request(quota_provider):
            flags = await ProcessingFlags.get_instance()
            if flags.allow_local_processing:
                model_override = "llama"
                provider_id = "ollama"
                model_name = None
                logger.info(
                    f"Quota exceeded for {provider_cfg.display_name if provider_cfg else provider_id}; switching to local llama"
                )
            else:
                raise HTTPException(
                    status_code=429,
                    detail=(
                        f"{provider_cfg.display_name if provider_cfg else provider_id} "
                        f"quota exhausted. Enable 'Allow local processing' in Settings."
                    ),
                )

        # Transform message through prompter (session-aware).
        transformed_prompt = await prompter_service.transform(
            raw_message=message_content,
            session_id=session_id,
            notebook_id=notebook.id if notebook else None,
            message_type="chat",
        )

        # Send via provider.
        send_result = await _send_via_provider(
            provider_id=provider_id,
            model_name=model_name,
            message=transformed_prompt,
            session_id=session_id,
            thinking_enabled=True,
            ref_file_ids=ref_file_ids or None,
            timeout=120.0,
        )

        if not send_result.ok:
            if send_result.manual_login_required:
                raise HTTPException(
                    status_code=403,
                    detail=(
                        f"{provider_cfg.display_name if provider_cfg else provider_id} "
                        f"session expired or invalid credentials. "
                        f"Re-log in and update credentials in Settings."
                    ),
                )
            if send_result.rate_limited:
                raise HTTPException(
                    status_code=429,
                    detail=(
                        f"{provider_cfg.display_name if provider_cfg else provider_id} "
                        f"rate limited. Wait for quota reset."
                    ),
                )
            raise HTTPException(
                status_code=502,
                detail=f"{provider_cfg.display_name if provider_cfg else provider_id} error: {send_result.error or 'unknown error'}",
            )

        response_text = send_result.response or ""
        thinking_text = send_result.thinking
        if not response_text and not thinking_text:
            raise HTTPException(
                status_code=502,
                detail=f"Empty response from {provider_cfg.display_name if provider_cfg else provider_id}.",
            )

        # Increment quota for this provider.
        await quota_tracker.increment(quota_provider, 1)

        # Build assistant message
        from langchain_core.messages import AIMessage

        assistant_message = AIMessage(
            content=response_text or "",
            id=str(uuid4()),
            additional_kwargs={"thinking": thinking_text} if thinking_text else {},
            additional_kwargs2={
                "provider": provider_id,
                "model": model_name or (provider_cfg.default_model if provider_cfg else None),
            } if (provider_id or model_name) else {},
        )

        # Persist the turn (edited human message already updated above)
        turn_messages = [assistant_message]
        try:
            added = await persist_chat_messages(full_session_id, turn_messages)
            if added:
                logger.info(
                    f"Persisted {added} chat message(s) for {full_session_id}"
                )
        except Exception as exc:
            logger.warning(f"Could not persist chat messages: {exc}")

        messages_out = extract_chat_messages(turn_messages)

        return EditMessageResponse(
            session_id=session_id,
            messages=messages_out,
            edit_count=new_edit_count,
            continuations_deleted=continuations_deleted,
        )

        # Non-deepseek-web: use LangGraph graph
        from langchain_core.messages import AIMessage, HumanMessage

        # Reconstruct truncated history from SurrealDB (messages up to and including edited one)
        truncated_history = messages[: target_idx + 1]
        # Replace the edited message content
        truncated_history[target_idx] = {
            **truncated_history[target_idx],
            "content": message_content,
        }

        # Build LangChain messages
        lc_messages: list[Any] = []
        for m in truncated_history:
            if m["role"] == "human":
                lc_messages.append(HumanMessage(content=m["content"], id=m["id"]))
            elif m["role"] == "ai":
                # Thinking not stored in SurrealDB transcript yet;
                # best-effort reconstruction without it.
                lc_messages.append(AIMessage(content=m["content"], id=m["id"]))

        state_values = {
            "messages": lc_messages,
            "context": context,
            "notebook": notebook,
            "model_override": model_override,
        }

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
            HumanMessage(content=message_content, id=str(uuid4())),
        )

        await session.save()

        turn_messages = result.get("messages", [])
        try:
            added = await persist_chat_messages(full_session_id, turn_messages)
            if added:
                logger.info(f"Persisted {added} chat message(s)")
        except Exception as exc:
            logger.warning(f"Could not persist chat messages: {exc}")

        messages_out = extract_chat_messages(turn_messages)

        return EditMessageResponse(
            session_id=session_id,
            messages=messages_out,
            edit_count=new_edit_count,
            continuations_deleted=continuations_deleted,
        )
    except NotFoundError:
        raise HTTPException(status_code=404, detail="Session not found")
    except HTTPException:
        raise
    except OpenNotebookError:
        raise
    except Exception as e:
        logger.error(
            f"Error editing chat message: {str(e)}\n"
            f"  Session ID: {session_id}\n"
            f"  Message ID: {message_id}\n"
            f"  Traceback:\n{traceback.format_exc()}"
        )
        raise HTTPException(status_code=500, detail=f"Error editing message: {str(e)}")


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
