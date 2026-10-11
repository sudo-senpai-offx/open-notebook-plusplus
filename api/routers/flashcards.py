from typing import List, Optional

from fastapi import APIRouter, HTTPException, Query
from loguru import logger
from pydantic import BaseModel

from open_notebook.database.repository import repo_query
from open_notebook.domain.flashcard import Flashcard, content_guid
from open_notebook.exceptions import (
    InvalidInputError,
    NotFoundError,
    OpenNotebookError,
)

router = APIRouter()


class FlashcardEdit(BaseModel):
    front: Optional[str] = None
    back: Optional[str] = None
    cloze_text: Optional[str] = None
    tags: Optional[List[str]] = None


def _response(card) -> dict:
    return {
        "id": str(card.id),
        "source_id": card.source_id,
        "model": card.model,
        "front": card.front,
        "back": card.back,
        "cloze_text": card.cloze_text,
        "tags": list(card.tags or []),
        "guid": card.guid,
        "status": card.status,
    }


async def _load_or_404(card_id: str):
    try:
        return await Flashcard.get(card_id)
    except NotFoundError:
        raise HTTPException(status_code=404, detail="Card not found")


@router.get("/flashcards")
async def list_cards(
    status: Optional[str] = Query(default=None),
    source_id: Optional[str] = Query(default=None),
    limit: int = Query(default=100, le=500),
):
    """Inbox/bank listing with status + source filters."""
    if status is not None and status not in ("draft", "approved", "tombstoned"):
        raise HTTPException(status_code=400, detail="Bad status filter")
    try:
        clauses = []
        if status is not None:
            clauses.append("status = $status")
        if source_id is not None:
            clauses.append("source_id = $source_id")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = await repo_query(
            f"SELECT * FROM flashcard {where} ORDER BY created DESC LIMIT $limit",
            {"status": status, "source_id": source_id, "limit": limit},
        )
        return [dict(r) for r in (rows or [])]
    except OpenNotebookError:
        raise
    except Exception as e:
        logger.warning(f"flashcard list failed: {e}")
        raise HTTPException(status_code=500, detail="Failed to list cards")


@router.post("/flashcards/{card_id}/approve")
async def approve_card(card_id: str):
    """Draft -> approved, stamping a stable GUID (needed for .apkg dedupe)."""
    card = await _load_or_404(card_id)
    if card.status != "draft":
        raise HTTPException(status_code=409, detail="Only drafts can be approved")
    try:
        card.guid = content_guid(card.model, card.front, card.back, card.cloze_text)
        card.status = "approved"
        await card.save()
        return _response(card)
    except InvalidInputError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except OpenNotebookError:
        raise
    except Exception as e:
        logger.warning(f"flashcard approve failed: {e}")
        raise HTTPException(status_code=500, detail="Failed to approve card")


@router.put("/flashcards/{card_id}")
async def edit_card(card_id: str, body: FlashcardEdit):
    """Edit fields while still a draft. Approved cards are immutable."""
    card = await _load_or_404(card_id)
    if card.status != "draft":
        raise HTTPException(status_code=409, detail="Only drafts can be edited")
    try:
        for field in ("front", "back", "cloze_text", "tags"):
            value = getattr(body, field)
            if value is not None:
                setattr(card, field, value)
        await card.save()
        return _response(card)
    except InvalidInputError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except OpenNotebookError:
        raise
    except Exception as e:
        logger.warning(f"flashcard edit failed: {e}")
        raise HTTPException(status_code=500, detail="Failed to edit card")


@router.post("/flashcards/{card_id}/reject")
async def reject_card(card_id: str):
    """Hard-delete drafts (nothing ever left the app). Approved cards must
    go through tombstone so exports stay consistent."""
    card = await _load_or_404(card_id)
    if card.status != "draft":
        raise HTTPException(
            status_code=409,
            detail="Approved cards cannot be rejected; use the tombstone endpoint",
        )
    try:
        await card.delete()
        return {"id": card_id, "deleted": True}
    except OpenNotebookError:
        raise
    except Exception as e:
        logger.warning(f"flashcard reject failed: {e}")
        raise HTTPException(status_code=500, detail="Failed to reject card")


@router.post("/flashcards/{card_id}/tombstone")
async def tombstone_card(card_id: str):
    """Exclude an approved card from reviews/exports. Anki-side copies must
    be deleted manually (stated in the UI copy, not enforced here)."""
    card = await _load_or_404(card_id)
    if card.status != "approved":
        raise HTTPException(status_code=409, detail="Only approved cards tombstone")
    try:
        card.status = "tombstoned"
        await card.save()
        return _response(card)
    except InvalidInputError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except OpenNotebookError:
        raise
    except Exception as e:
        logger.warning(f"flashcard tombstone failed: {e}")
        raise HTTPException(status_code=500, detail="Failed to tombstone card")
