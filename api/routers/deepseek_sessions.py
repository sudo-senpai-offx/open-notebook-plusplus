"""DeepSeek session series API (A1, A3, A5).

Endpoints:
  POST   /api/deepseek-sessions        — list or create a DeepSeek session
  DELETE /api/deepseek-sessions/{id}   — one-way cascade: archive ON++ row
                                         + delete DeepSeek web session
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query
from loguru import logger
from pydantic import BaseModel, Field

from open_notebook.domain.deepseek_session import DeepSeekSession
from open_notebook.exceptions import OpenNotebookError
from open_notebook.services.deepseek_session_service import (
    deepseek_session_service,
)

router = APIRouter()


class DeepSeekSessionCreate(BaseModel):
    notebook_id: str = Field(..., description="Notebook ID")
    classification: str = Field(
        ...,
        description="chat | insight | embedding | tiny_training | podcast | flashcard | stt | tts | search | source_chat",
    )


class DeepSeekSessionResponse(BaseModel):
    id: str
    notebook_id: Optional[str]
    deepseek_session_id: Optional[str]
    classification: Optional[str]
    token_count: int
    parent_deepseek_session_id: Optional[str]
    status: str
    created_at: Optional[str]
    last_used_at: Optional[str]


@router.post("/deepseek-sessions", response_model=List[DeepSeekSessionResponse])
async def list_or_create_deepseek_sessions(
    request: Optional[DeepSeekSessionCreate] = None,
    notebook_id: Optional[str] = Query(
        None, description="Filter by notebook id (when listing)"
    ),
    classification: Optional[str] = Query(
        None, description="Filter by classification (when listing)"
    ),
):
    """List DeepSeek sessions for a notebook, or create a new one."""
    try:
        # Create path: body supplied with notebook_id + classification.
        if request is not None:
            session = DeepSeekSession(
                notebook_id=request.notebook_id,
                deepseek_session_id=None,
                classification=request.classification,
                token_count=0,
                parent_deepseek_session_id=None,
                status="active",
            )
            await session.save()
            return [
                DeepSeekSessionResponse(
                    id=session.id or "",
                    notebook_id=session.notebook_id,
                    deepseek_session_id=session.deepseek_session_id,
                    classification=session.classification,
                    token_count=session.token_count,
                    parent_deepseek_session_id=session.parent_deepseek_session_id,
                    status=session.status,
                    created_at=str(session.created_at) if session.created_at else None,
                    last_used_at=(
                        str(session.last_used_at) if session.last_used_at else None
                    ),
                )
            ]

        # List path: query params.
        nb = notebook_id or ""
        if not nb:
            raise HTTPException(
                status_code=400,
                detail="notebook_id is required when listing deepseek sessions",
            )

        sessions = await deepseek_session_service.list_for_notebook(
            nb, classification=classification
        )
        return [
            DeepSeekSessionResponse(
                id=s.id or "",
                notebook_id=s.notebook_id,
                deepseek_session_id=s.deepseek_session_id,
                classification=s.classification,
                token_count=s.token_count,
                parent_deepseek_session_id=s.parent_deepseek_session_id,
                status=s.status,
                created_at=str(s.created_at) if s.created_at else None,
                last_used_at=str(s.last_used_at) if s.last_used_at else None,
            )
            for s in sessions
        ]
    except HTTPException:
        raise
    except OpenNotebookError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:  # noqa: BLE001
        logger.error(f"DeepSeek sessions error: {e}")
        raise HTTPException(status_code=500, detail=f"DeepSeek sessions failed: {e}")


@router.delete("/deepseek-sessions/{session_id}", response_model=Dict[str, Any])
async def delete_deepseek_session(session_id: str):
    """One-way delete cascade (A3): archive ON++ row + delete DeepSeek web session.

    ON++ deletion cascades to DeepSeek. DeepSeek deletion never cascades back.
    """
    try:
        ok = await deepseek_session_service.delete_session(session_id)
        if not ok:
            raise HTTPException(status_code=404, detail="DeepSeek session not found")
        return {"ok": True, "message": "DeepSeek session deleted (one-way cascade)"}
    except HTTPException:
        raise
    except OpenNotebookError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:  # noqa: BLE001
        logger.error(f"Delete deepseek session {session_id} error: {e}")
        raise HTTPException(
            status_code=500, detail=f"Failed to delete deepseek session: {e}"
        )
