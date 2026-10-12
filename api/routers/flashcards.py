from typing import List, Optional

from fastapi import APIRouter, HTTPException, Query
from loguru import logger
from pydantic import BaseModel, Field

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


class FlashcardCreate(BaseModel):
    model: str = Field(pattern="^(basic|cloze)$")
    front: Optional[str] = None
    back: Optional[str] = None
    cloze_text: Optional[str] = None
    tags: List[str] = Field(default_factory=list)
    source_id: Optional[str] = None


class FlashcardImport(BaseModel):
    """Anki-compatible TSV import. Basic rows: front<TAB>back[<TAB>tags,].
    Cloze rows: one cloze text per line. Extras travel as tags."""

    format: str = Field(pattern="^(basic|cloze)$")
    text: str = Field(max_length=200_000)
    delimiter: str = Field(default="\t", max_length=1)


MAX_IMPORT_ROWS = 500


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


@router.post("/flashcards")
async def create_card(body: FlashcardCreate):
    """Author a card manually. Always lands as a draft for the approval gate."""
    if body.model == "basic" and not (body.front or "").strip():
        raise HTTPException(status_code=400, detail="Basic cards need a front")
    if body.model == "cloze" and not (body.cloze_text or "").strip():
        raise HTTPException(status_code=400, detail="Cloze cards need cloze_text")
    try:
        card = Flashcard(
            model=body.model,  # type: ignore[arg-type]
            front=body.front,
            back=body.back,
            cloze_text=body.cloze_text,
            tags=list(body.tags or []),
            source_id=body.source_id,
            status="draft",
        )
        await card.save()
        return _response(card)
    except InvalidInputError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except OpenNotebookError:
        raise
    except Exception as e:
        logger.warning(f"flashcard create failed: {e}")
        raise HTTPException(status_code=500, detail="Failed to create card")


@router.post("/flashcards/import")
async def import_cards(body: FlashcardImport):
    """Bulk-create drafts from TSV text. Returns per-row report; drafts only."""
    try:
        lines = [ln for ln in body.text.splitlines() if ln.strip()]
        if len(lines) > MAX_IMPORT_ROWS:
            raise HTTPException(
                status_code=400,
                detail=f"Too many rows ({len(lines)} > {MAX_IMPORT_ROWS})",
            )
        created = 0
        skipped = []
        for index, line in enumerate(lines):
            try:
                if body.format == "basic":
                    parts = line.split(body.delimiter)
                    front = parts[0].strip() if len(parts) > 0 else ""
                    back = parts[1].strip() if len(parts) > 1 else ""
                    tags = (
                        [t.strip() for t in parts[2].split(",") if t.strip()]
                        if len(parts) > 2
                        else []
                    )
                    if not front:
                        raise ValueError("missing front")
                    card = Flashcard(
                        model="basic", front=front, back=back or None, tags=tags
                    )
                else:
                    text = line.strip()
                    if not text:
                        raise ValueError("empty line")
                    card = Flashcard(model="cloze", cloze_text=text)
                await card.save()
                created += 1
            except (ValueError, InvalidInputError) as e:
                skipped.append({"row": index + 1, "reason": str(e)})
        return {"created": created, "skipped": skipped}
    except HTTPException:
        raise
    except OpenNotebookError:
        raise
    except Exception as e:
        logger.warning(f"flashcard import failed: {e}")
        raise HTTPException(status_code=500, detail="Failed to import cards")


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


@router.get("/flashcards/export")
async def export_apkg(deck: str = Query(default="OpenNotebook")):
    """Export approved cards as .apkg (tombstones excluded). The file lands
    in the data exports dir for re-download/sharing."""
    import os
    import re
    import time

    from fastapi.responses import FileResponse

    from open_notebook.export.apkg import build_package_bytes

    safe_deck = re.sub(r"[^A-Za-z0-9_-]+", "_", deck).strip("_") or "OpenNotebook"
    try:
        rows = await repo_query(
            "SELECT model, front, back, cloze_text, tags, guid FROM flashcard "
            "WHERE status = 'approved' ORDER BY created ASC LIMIT 5000"
        )
        cards = [dict(r) for r in (rows or [])]
        data = build_package_bytes(cards, deck_name=safe_deck)
        base = os.getenv("OPEN_NOTEBOOK_DATA_DIR", "./notebook_data")
        out_dir = os.path.join(base, "exports")
        os.makedirs(out_dir, exist_ok=True)
        filename = f"{safe_deck}-{int(time.time())}.apkg"
        path = os.path.join(out_dir, filename)
        with open(path, "wb") as f:
            f.write(data)
        return FileResponse(
            path,
            media_type="application/octet-stream",
            filename=filename,
        )
    except OpenNotebookError:
        raise
    except Exception as e:
        logger.warning(f"flashcard export failed: {e}")
        raise HTTPException(status_code=500, detail="Failed to export deck")


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
