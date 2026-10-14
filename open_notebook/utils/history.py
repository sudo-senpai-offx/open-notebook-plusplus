"""Decide which part of a conversation the model actually sees.

The problem
-----------
`execute_chat` loaded the entire checkpoint history and sent all of it,
every turn, with no trimming. Measured on the phone's configuration
(`llama-server -c 4096`):

    system prompt, bare                 618 tokens
    plus 6k of retrieved context      1,404 tokens
    reserving room for the reply       256 tokens
    --------------------------------------------
    room for history                  2,436 tokens

A real exchange, with citations, runs a few hundred tokens. That is
roughly ten to twenty-five exchanges before the prompt no longer fits -
reached over a couple of weeks of study, and reached *silently*: the
server either truncates or refuses, and neither says "your conversation
got too long".

Two ways to be shorter
----------------------
**By budget.** The default, and it always applies. Take the newest
exchanges that fit, and never split one - a model shown a bare answer
with no question in front of it is worse than one shown a shorter,
complete conversation.

**By timeframe.** The caller can ask for history since a moment
(`history_since`). That is a different tool: not "as much as fits" but
"last night's session, please". A study conversation spans days, and the
exchange from Tuesday may matter more than the four from this morning
regardless of budget.

The report returned alongside says what was dropped, so the UI can tell
you rather than the conversation quietly losing its beginnings.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, List, Optional, Sequence

from loguru import logger

from open_notebook.utils.token_utils import token_count

# The window llama-server was started with. Must match `-c`; the launcher
# exports it so the two cannot drift.
DEFAULT_CONTEXT_TOKENS = 4096

# Held back so the model has somewhere to write. A reply that runs out of
# room mid-sentence is worse than a short answer that finishes.
REPLY_RESERVE_TOKENS = 256

# A turn shorter than this is not worth counting separately - it is
# almost always "ok" or a two-word follow-up, and measuring each one
# costs more than it buys.
_MIN_WORTHFUL_TURN = 8


@dataclass
class HistorySelection:
    """What was kept, what was dropped, and why."""

    messages: List[Any] = field(default_factory=list)
    kept_exchanges: int = 0
    dropped_exchanges: int = 0
    dropped_tokens: int = 0
    used_tokens: int = 0
    budget_tokens: int = 0
    reason: str = "budget"

    def summary(self) -> dict:
        return {
            "kept_exchanges": self.kept_exchanges,
            "dropped_exchanges": self.dropped_exchanges,
            "used_tokens": self.used_tokens,
            "budget_tokens": self.budget_tokens,
            "reason": self.reason,
        }


def _as_utc(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _turn_text(message: Any) -> str:
    content = getattr(message, "content", "")
    return content if isinstance(content, str) else str(content)


def _turn_tokens(message: Any) -> int:
    text = _turn_text(message)
    return token_count(text) if text else 0


def split_exchanges(messages: Sequence[Any]) -> List[List[Any]]:
    """Group into whole exchanges: a question and the answer it got.

    Trimming inside an exchange is the one thing this must never do. A
    model handed an answer with its question cut off answers something
    nobody asked, which is worse than it not remembering.
    """
    groups: List[List[Any]] = []
    pending: Optional[List[Any]] = None

    for message in messages or []:
        is_question = str(getattr(message, "type", "")) == "human"
        if is_question or pending is None:
            if pending:
                groups.append(pending)
            pending = [message]
        else:
            pending.append(message)

    if pending:
        groups.append(pending)
    return groups


def select_history(
    messages: Sequence[Any],
    context_tokens: int = DEFAULT_CONTEXT_TOKENS,
    history_since: Optional[Any] = None,
    reserve: int = REPLY_RESERVE_TOKENS,
) -> HistorySelection:
    """Choose which exchanges to send, newest first, whole exchanges only.

    *history_since* takes precedence when set and everything older is
    dropped; the budget then still applies within the window, so a very
    long afternoon cannot blow the context either.
    """
    groups = split_exchanges(messages)
    total = len(groups)
    reason = "budget"

    if history_since is not None:
        # Accepts a datetime or the shorthand "45m" / "2d" / "1w".
        # Parsing here rather than trusting the caller: the router already
        # calls parse_since, and with only that in place a raw string
        # reached this function, failed to parse, and was silently
        # ignored - so the timeframe control quietly did nothing.
        cutoff = (
            history_since
            if isinstance(history_since, datetime)
            else parse_since(str(history_since))
        )
        if cutoff is None:
            logger.warning(f"Unparseable history_since {history_since!r}; ignoring it")
        else:
            groups = [group for group in groups if _within(group, cutoff)]
            reason = "timeframe"

    # The budget applies either way. A timeframe narrows the window; it
    # does not exempt it. Returning straight after the filter meant a
    # long afternoon selected with "since this morning" was sent whole,
    # at 26,800 tokens into a 1,792-token budget - the prompt simply
    # would not have fitted.
    budget = max(0, context_tokens - reserve)
    kept: List[List[Any]] = []
    used = 0

    for group in reversed(groups):
        cost = sum(_turn_tokens(m) for m in group)
        if used + cost > budget and kept:
            break
        kept.append(group)
        used += cost
        if used >= budget:
            break

    kept.reverse()
    return _finish(kept, context_tokens, reserve, total - len(kept), reason)


def _within(group: Sequence[Any], cutoff: datetime) -> bool:
    """True when the exchange is inside the window, or carries no date.

    A conversation that predates timestamps in its checkpoint must not be
    emptied by the timeframe control - that would turn "narrow this" into
    "delete everything", which is not what anyone asking for it means.
    """
    stamp = _first_timestamp(group)
    return stamp is None or stamp >= cutoff


def _first_timestamp(group: Sequence[Any]) -> Optional[datetime]:
    for message in group:
        for attribute in ("created", "timestamp", "created_at"):
            stamp = _as_utc(getattr(message, attribute, None))
            if stamp is not None:
                return stamp
        metadata = getattr(message, "response_metadata", None)
        if isinstance(metadata, dict):
            stamp = _as_utc(metadata.get("created"))
            if stamp is not None:
                return stamp
    return None


def _finish(
    groups: List[List[Any]],
    context_tokens: int,
    reserve: int,
    dropped: int,
    reason: str,
) -> HistorySelection:
    flattened: List[Any] = [m for group in groups for m in group]
    used = sum(_turn_tokens(m) for m in flattened)
    return HistorySelection(
        messages=flattened,
        kept_exchanges=len(groups),
        dropped_exchanges=dropped,
        dropped_tokens=0,
        used_tokens=used,
        budget_tokens=max(0, context_tokens - reserve),
        reason=reason,
    )


def parse_since(value: Optional[str]) -> Optional[datetime]:
    """Accept a timestamp, or a shorthand like ``2d`` / ``36h`` / ``45m``."""
    if not value:
        return None
    text = str(value).strip().lower()

    if len(text) > 1 and text[-1] in ("m", "h", "d", "w"):
        amount, unit = text[:-1], text[-1]
        if amount.isdigit():
            delta = {
                "m": timedelta(minutes=int(amount)),
                "h": timedelta(hours=int(amount)),
                "d": timedelta(days=int(amount)),
                "w": timedelta(weeks=int(amount)),
            }[unit]
            return datetime.now(timezone.utc) - delta

    return _as_utc(text)
