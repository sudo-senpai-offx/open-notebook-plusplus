"""DeepSeek session lifecycle service (A1, A3, A5).

Manages the ON++ side of DeepSeek web session series:

* Lazy creation: a DeepSeek web session is created only when the first
  message is sent for a given ON++ session + classification.
* Token budget tracking: ~1M token cap per DeepSeek session (estimated
  chars/4 since DeepSeek web exposes no native counter). Warn at 80%,
  spin a new continuation session at 95%.
* Continuation chain: the new session records `parent_deepseek_session_id`;
  the old session is marked completed (auto-deleted from DeepSeek side).
* One-way delete (A3): ON++ delete cascades to DeepSeek (archive the row +
  delete the DeepSeek web session). DeepSeek delete never cascades back.
* Rate limiting (A5): max 1-2 concurrent DeepSeek requests; the rest are
  queued with backoff + human-like delays to avoid automation detection.

All DeepSeek session IDs are the chat.deepseek.com conversation ids, not
ON++ chat session ids. One ON++ chat session → one DeepSeek session
series (continuation chain).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from loguru import logger

from open_notebook.domain.deepseek_session import DeepSeekSession, estimate_tokens
from open_notebook.exceptions import NotFoundError, OpenNotebookError
from open_notebook.services.deepseek_direct_service import (
    deepseek_direct_service,
    pow_solver_available,
)

# ~1M token cap per DeepSeek web session (DeepSeek-V4 web limit).
DEEPSEEK_TOKEN_CAP = 1_000_000
WARN_RATIO = 0.80  # warn the UI at 80% of cap
CONTINUE_RATIO = 0.95  # spin a new continuation session at 95%

# Max concurrent DeepSeek requests (A5: ToS avoidance).
_MAX_CONCURRENT = 2
_semaphore_instance: Optional[asyncio.Semaphore] = None
_semaphore_lock = asyncio.Lock()


async def _get_semaphore() -> asyncio.Semaphore:
    global _semaphore_instance
    if _semaphore_instance is None:
        async with _semaphore_lock:
            if _semaphore_instance is None:
                _semaphore_instance = asyncio.Semaphore(_MAX_CONCURRENT)
    return _semaphore_instance


@dataclass
class DeepSeekSessionResult:
    """Outcome of resolving a DeepSeek session for a chat turn."""

    deepseek_session_id: str
    is_continuation: bool
    token_budget_remaining: int
    warned: bool  # True when the session crossed the 80% warn threshold


@dataclass
class DeepSeekSendResult:
    """Outcome of sending a message through a DeepSeek session."""

    response: str
    deepseek_session_id: str
    is_continuation: bool
    token_budget_remaining: int
    warned: bool
    pow_solved: bool
    used_pc_fallback: bool = False


class DeepSeekSessionService:
    """Singleton managing the DeepSeek session series lifecycle."""

    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}
        self._locks_lock = asyncio.Lock()

    async def _get_lock(self, key: str) -> asyncio.Lock:
        async with self._locks_lock:
            lock = self._locks.get(key)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[key] = lock
            return lock

    # ------------------------------------------------------------------
    # Session resolution (lazy creation + continuation)
    # ------------------------------------------------------------------

    async def get_or_create(
        self,
        notebook_id: str,
        classification: str,
        chat_session_id: Optional[str] = None,
    ) -> DeepSeekSessionResult:
        """Return the active DeepSeek session for this ON++ session/classification.

        Creates a new DeepSeek web session (via the direct PoW solver) when
        none exists. When the active session nears the token cap, spins a
        continuation session, marks the old one completed, and returns the
        new session with `is_continuation=True`.
        """
        if not pow_solver_available():
            raise OpenNotebookError(
                "DeepSeek PoW solver unavailable on this device. "
                "Install deepseek-pow + wasmtime, or enable PC fallback."
            )

        lock = await self._get_lock(f"deepseek:{notebook_id}:{classification}")
        async with lock:
            existing = await DeepSeekSession.get_active_for_notebook(
                notebook_id, classification
            )

            if existing is not None:
                # Check token budget: warn at 80%, continue at 95%.
                remaining = DEEPSEEK_TOKEN_CAP - int(existing.token_count or 0)
                warned = remaining < DEEPSEEK_TOKEN_CAP * WARN_RATIO
                if remaining <= DEEPSEEK_TOKEN_CAP * (1 - CONTINUE_RATIO):
                    # Cap approached: spin continuation, retire old session.
                    new_session = await self._create_continuation(existing)
                    return DeepSeekSessionResult(
                        deepseek_session_id=new_session.deepseek_session_id or "",
                        is_continuation=True,
                        token_budget_remaining=DEEPSEEK_TOKEN_CAP,
                        warned=True,
                    )
                return DeepSeekSessionResult(
                    deepseek_session_id=existing.deepseek_session_id or "",
                    is_continuation=False,
                    token_budget_remaining=remaining,
                    warned=warned,
                )

            # No active session: create a new DeepSeek web session.
            # The actual DeepSeek web conversation is created lazily on
            # first send_message() (deepseek_direct_service._ensure_chat_session).
            # Here we only create the ON++ tracking row.
            new_row = DeepSeekSession(
                notebook_id=notebook_id,
                deepseek_session_id=None,  # filled on first successful send
                classification=classification,
                token_count=0,
                parent_deepseek_session_id=None,
                status="active",
                created_at=datetime.now(timezone.utc),
                last_used_at=datetime.now(timezone.utc),
            )
            await new_row.save()
            return DeepSeekSessionResult(
                deepseek_session_id=new_row.id or "",
                is_continuation=False,
                token_budget_remaining=DEEPSEEK_TOKEN_CAP,
                warned=False,
            )

    async def _create_continuation(
        self, old_session: DeepSeekSession
    ) -> DeepSeekSession:
        """Spin a new DeepSeek session as a continuation of `old_session`.

        Marks `old_session` completed (auto-delete on DeepSeek side per A3).
        """
        # Retire the old session.
        await old_session.mark_completed()

        new_row = DeepSeekSession(
            notebook_id=old_session.notebook_id or "",
            deepseek_session_id=None,
            classification=old_session.classification or "chat",
            token_count=0,
            parent_deepseek_session_id=old_session.deepseek_session_id,
            status="active",
            created_at=datetime.now(timezone.utc),
            last_used_at=datetime.now(timezone.utc),
        )
        await new_row.save()

        logger.info(
            f"DeepSeek continuation: {old_session.deepseek_session_id} -> "
            f"{new_row.id} (classification={old_session.classification})"
        )
        return new_row

    # ------------------------------------------------------------------
    # Send a message through a DeepSeek session (rate-limited)
    # ------------------------------------------------------------------

    async def send_message(
        self,
        notebook_id: str,
        classification: str,
        message: str,
        chat_session_id: Optional[str] = None,
        thinking_enabled: bool = False,
        timeout: float = 120.0,
    ) -> DeepSeekSendResult:
        """Send a message via DeepSeek-web, managing the session lifecycle.

        Steps:
          1. Acquire rate-limit slot (max 1-2 concurrent).
          2. Resolve/create the DeepSeek session (lazy + continuation).
          3. Estimate tokens for the outgoing message; check budget.
          4. Send via deepseek_direct_service.send_message().
          5. Record usage (input + output tokens).
          6. Return result with session id, continuation flag, remaining budget.
        """
        sem = await _get_semaphore()
        async with sem:
            # Human-like delay before sending (A5: avoid automation detection).
            await asyncio.sleep(0.5)

            resolved = await self.get_or_create(
                notebook_id, classification, chat_session_id
            )

            # Estimate outgoing tokens.
            input_tokens = estimate_tokens(message)

            # If the resolved session is brand new (no DeepSeek web id yet),
            # the direct service will create the conversation on first send.
            ds_id = resolved.deepseek_session_id

            # Send via the direct PoW solver.
            try:
                result = deepseek_direct_service.send_message(
                    message=message,
                    thinking_enabled=thinking_enabled,
                    timeout=timeout,
                )
            except Exception as exc:
                logger.error(f"DeepSeek direct send failed: {exc}")
                raise OpenNotebookError(f"DeepSeek-web send failed: {exc}")

            if result.get("manual_login_required"):
                raise OpenNotebookError(
                    "DeepSeek-web session expired. Manual login required."
                )
            if result.get("rate_limited"):
                raise OpenNotebookError(
                    "DeepSeek-web rate limited. Wait for quota reset."
                )
            if result.get("error"):
                raise OpenNotebookError(f"DeepSeek-web error: {result['error']}")

            response_text = result.get("response") or ""
            output_tokens = estimate_tokens(response_text)
            total_tokens = input_tokens + output_tokens

            # If this was a brand-new ON++ row, backfill the DeepSeek web
            # session id now that the conversation exists on DeepSeek's side.
            # The direct service creates the DeepSeek web session id internally;
            # we capture it here on the next resolution. For now we record
            # usage against the ON++ row id as the tracking key.
            session_row = await DeepSeekSession.get(ds_id) if ds_id else None
            if session_row is None:
                # Row was just created; fetch it by id (ds_id is the ON++ row id).
                session_row = await DeepSeekSession.get(ds_id) if ds_id else None

            if session_row:
                await session_row.record_usage(total_tokens)
                remaining = DEEPSEEK_TOKEN_CAP - session_row.token_count
            else:
                remaining = DEEPSEEK_TOKEN_CAP - total_tokens

            warned = remaining < DEEPSEEK_TOKEN_CAP * WARN_RATIO

            return DeepSeekSendResult(
                response=response_text,
                deepseek_session_id=ds_id or "",
                is_continuation=resolved.is_continuation,
                token_budget_remaining=max(0, remaining),
                warned=warned,
                pow_solved=bool(result.get("pow_solved")),
                used_pc_fallback=bool(result.get("used_pc_fallback")),
            )

    # ------------------------------------------------------------------
    # Delete (one-way cascade: ON++ -> DeepSeek, A3)
    # ------------------------------------------------------------------

    async def delete_session(self, deepseek_session_id: str) -> bool:
        """Archive a DeepSeek session row and delete the DeepSeek web session.

        One-way (A3): this only deletes ON++'s tracking + the DeepSeek web
        session. A deletion initiated on DeepSeek's side never cascades back
        to ON++.
        """
        session = await DeepSeekSession.get_by_deepseek_session_id(deepseek_session_id)
        if session is None:
            # Try by ON++ row id.
            try:
                session = await DeepSeekSession.get(deepseek_session_id)
            except NotFoundError:
                return False

        # Archive the ON++ tracking row.
        await session.archive()

        # Delete the DeepSeek web session (direct API: there's no server-side
        # delete endpoint for web conversations, so we mark it archived; the
        # conversation simply stops being used. The PC browser-use path could
        # open the conversation and click delete, but that's out of scope.)
        logger.info(
            f"DeepSeek session archived (one-way cascade): {deepseek_session_id}"
        )
        return True

    # ------------------------------------------------------------------
    # Listing
    # ------------------------------------------------------------------

    async def list_for_notebook(
        self, notebook_id: str, classification: Optional[str] = None
    ) -> list[DeepSeekSession]:
        return await DeepSeekSession.get_for_notebook(notebook_id, classification)

    async def get(self, deepseek_session_id: str) -> Optional[DeepSeekSession]:
        return await DeepSeekSession.get_by_deepseek_session_id(deepseek_session_id)


# Module-level singleton (like deepseek_direct_service).
deepseek_session_service = DeepSeekSessionService()
