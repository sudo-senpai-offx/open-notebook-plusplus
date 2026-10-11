from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException
from loguru import logger
from pydantic import BaseModel, Field

from open_notebook.database.repository import repo_query
from open_notebook.domain.focus import FocusSession
from open_notebook.exceptions import (
    InvalidInputError,
    NotFoundError,
    OpenNotebookError,
)

router = APIRouter()


class FocusSessionStart(BaseModel):
    notebook_id: Optional[str] = None
    preset: str = "pomodoro"
    planned_seconds: int = Field(default=1500, ge=60)


class FocusSessionEnd(BaseModel):
    status: str = Field(default="done", pattern="^(done|cancelled)$")


def _response(session: FocusSession) -> dict:
    return {
        "id": str(session.id),
        "notebook_id": session.notebook_id,
        "preset": session.preset,
        "planned_seconds": session.planned_seconds,
        "status": session.status,
    }


@router.post("/focus/sessions")
async def start_session(body: FocusSessionStart):
    """Register a focus session lease (vetoes hibernate while running)."""
    try:
        session = FocusSession(
            notebook_id=body.notebook_id,
            preset=body.preset,
            planned_seconds=body.planned_seconds,
            status="running",
            started_at=datetime.now(timezone.utc),
        )
        await session.save()
        return _response(session)
    except InvalidInputError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except OpenNotebookError:
        raise
    except Exception as e:
        logger.warning(f"focus start failed: {e}")
        raise HTTPException(status_code=500, detail="Failed to start session")


@router.post("/focus/sessions/{session_id}/end")
async def end_session(session_id: str, body: FocusSessionEnd):
    """End a session (done/cancelled). Rows are never edited otherwise."""
    try:
        session = await FocusSession.get(session_id)
        if session.status != "running":
            raise HTTPException(status_code=409, detail="Session already ended")
        session.status = body.status  # type: ignore[assignment]
        session.ended_at = datetime.now(timezone.utc)
        await session.save()
        return _response(session)
    except HTTPException:
        raise
    except NotFoundError:
        raise HTTPException(status_code=404, detail="Session not found")
    except InvalidInputError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except OpenNotebookError:
        raise
    except Exception as e:
        logger.warning(f"focus end failed: {e}")
        raise HTTPException(status_code=500, detail="Failed to end session")


@router.get("/focus/sessions/open")
async def open_sessions():
    """Running sessions (UI recovery + governor lease source)."""
    try:
        rows = await repo_query(
            "SELECT id, notebook_id, preset, planned_seconds, status, started_at "
            "FROM focus_session WHERE status = 'running' ORDER BY started_at DESC"
        )
        return [
            {
                "id": str(r.get("id")),
                "notebook_id": r.get("notebook_id"),
                "preset": r.get("preset"),
                "planned_seconds": r.get("planned_seconds"),
                "status": r.get("status"),
            }
            for r in (rows or [])
        ]
    except OpenNotebookError:
        raise
    except Exception as e:
        logger.warning(f"focus open-list failed: {e}")
        raise HTTPException(status_code=500, detail="Failed to list sessions")
