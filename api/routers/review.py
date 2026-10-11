from fastapi import APIRouter, HTTPException, Query
from loguru import logger
from pydantic import BaseModel, Field

from open_notebook.exceptions import OpenNotebookError
from open_notebook.review import store

router = APIRouter()


class GradeRequest(BaseModel):
    rating: str = Field(pattern="^(again|hard|good|easy)$")
    duration_ms: int = Field(default=0, ge=0)


@router.get("/review/due")
async def due_cards(limit: int = Query(default=50, le=200)):
    """Cards due now (approved, scheduled past). Store-backed next slice."""
    try:
        return await store.list_due_cards(limit=limit)
    except OpenNotebookError:
        raise
    except Exception as e:
        logger.warning(f"review due failed: {e}")
        raise HTTPException(status_code=500, detail="Failed to list due cards")


@router.post("/review/grade/{card_id}")
async def grade_card_endpoint(card_id: str, body: GradeRequest):
    """Record one grade and advance FSRS state."""
    try:
        return await store.record_grade(
            card_id, body.rating, duration_ms=body.duration_ms
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except OpenNotebookError:
        raise
    except Exception as e:
        logger.warning(f"review grade failed: {e}")
        raise HTTPException(status_code=500, detail="Failed to record grade")


@router.get("/review/retention")
async def retention_target():
    """The locked scheduling target (R5Q4: 0.85, per-deck dial later)."""
    from open_notebook.review.scheduler import DESIRED_RETENTION

    return {"desired_retention": DESIRED_RETENTION}
