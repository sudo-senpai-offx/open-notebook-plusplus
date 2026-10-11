"""FSRS scheduling service (P4). Lazy torch-free import, 0.85 retention.

`py-fsrs` base is typing-extensions only — safe to import at module load, but
kept function-local anyway so non-review boots pay nothing. Card state
travels as JSON; the store slice persists it.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Tuple, cast

DESIRED_RETENTION = 0.85

_RATINGS = {"again": 1, "hard": 2, "good": 3, "easy": 4}


def _scheduler():
    from fsrs import Scheduler

    return Scheduler(desired_retention=DESIRED_RETENTION)


def grade_card(
    card_json: Dict[str, Any], rating: str
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Apply one FSRS review. Returns (card_json, log_json)."""
    if rating not in _RATINGS:
        raise ValueError(f"unknown rating: {rating!r} (again|hard|good|easy)")
    from fsrs import Card, Rating
    from fsrs.card import CardDict

    card = Card.from_dict(cast(CardDict, card_json)) if card_json else Card()
    new_card, log = _scheduler().review_card(card, Rating(_RATINGS[rating]))
    return new_card.to_dict(), log.to_dict()


def is_due(card_json: Dict[str, Any], now: datetime) -> bool:
    """Whether a stored card JSON is due at `now`."""
    due_raw = (card_json or {}).get("due")
    if not due_raw:
        return True
    try:
        due = datetime.fromisoformat(str(due_raw))
    except ValueError:
        return True
    if due.tzinfo is None:
        due = due.replace(tzinfo=timezone.utc)
    return due <= now
