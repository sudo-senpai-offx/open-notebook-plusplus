"""Review store seam (P4). Queries target the planned schema:

- `flashcard.fsrs_json` (FSRS Card JSON, null until first review)
- `revlog` table (card_id, rating, reviewed_at, duration_ms)

Both land in the storage slice (migration); until then these functions are
only exercised behind mocks. Nothing else may assume the schema.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List

from open_notebook.database.repository import repo_query
from open_notebook.review.scheduler import grade_card, is_due


async def list_due_cards(limit: int = 50) -> List[Dict[str, Any]]:
    """Approved, non-tombstoned cards whose FSRS state is due (or unreviewed)."""
    rows = await repo_query(
        "SELECT id, model, front, back, cloze_text, tags, guid, fsrs_json "
        "FROM flashcard WHERE status = 'approved' LIMIT $limit",
        {"limit": limit},
    )
    now = datetime.now(timezone.utc)
    due = []
    for r in rows or []:
        state = r.get("fsrs_json") or {}
        if is_due(state, now):
            due.append(
                {
                    "id": str(r.get("id")),
                    "model": r.get("model"),
                    "front": r.get("front"),
                    "back": r.get("back"),
                    "cloze_text": r.get("cloze_text"),
                    "tags": r.get("tags") or [],
                    "guid": r.get("guid"),
                }
            )
    return due


async def record_grade(
    card_id: str, rating: str, duration_ms: int = 0
) -> Dict[str, Any]:
    """Apply one grade: advance FSRS state on the card, append a revlog row."""
    from open_notebook.domain.flashcard import Flashcard

    card = await Flashcard.get(card_id)
    if card.status != "approved":
        raise ValueError("only approved cards can be graded")
    state = card.fsrs_json or {}
    new_state, log = grade_card(state, rating)
    card.fsrs_json = new_state
    await card.save()
    await repo_query(
        "CREATE revlog SET card_id = $card_id, rating = $rating, "
        "reviewed_at = time::now(), duration_ms = $duration_ms",
        {"card_id": card_id, "rating": rating, "duration_ms": duration_ms},
    )
    return {"id": card_id, "due": new_state.get("due")}
