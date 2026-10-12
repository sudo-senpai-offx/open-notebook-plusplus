"""Deep command-retry visibility (B2).

Library retries (e.g. ``run_transformation``'s 5 attempts) log every attempt
at DEBUG so a conflict storm does not flood the worker log. The worker runs
at INFO (``surreal_commands.core.worker.configure_logging``), so those
attempts are normally invisible: a command stuck at attempt 4/5 looks
identical to a command that is merely slow.

``process_source`` runs a single library attempt since B5; its per-error
budgets (15x DB conflicts, 3x other transients) log at DEBUG directly from
``execute_with_budgets``. This handler watches the library's retry records
and prints only *milestone* attempts (default every 5th) together with
the running retry distribution.

This module adds one extra loguru handler that watches the library's retry
records and prints only *milestone* attempts (default every 5th) together with
the running retry distribution. A deep-queue stall becomes visible without
enabling every attempt. The library's retry loop is untouched.

Behaviour:

* Installed once per process, at ``commands`` import (the worker imports that
  package before it connects to SurrealDB, so the handler survives
  ``configure_logging``).
* ``OPEN_NOTEBOOK_RETRY_VISIBILITY_STEP`` (default 5) controls the milestone
  distance; ``0`` / ``off`` / ``none`` disables the handler entirely.
* Never raises and never re-logs: loguru is not re-entrant, so the handler only
  filters and formats records (it does not call ``logger.*`` from a sink).
"""

import os
import re
import sys
from typing import Any, Callable, NamedTuple, Optional

from loguru import logger

DEFAULT_STEP = 5

#: ``[Retry] Attempt 4 failed with DatabaseOperationError: <msg>, waiting 8.0s before retry 5``
_RETRY_RE = re.compile(
    r"^\[Retry\] Attempt (\d+) failed with ([^:]+): (.*), "
    r"waiting ([\d.]+)s before retry (\d+)$"
)

_installed_handler_id: Optional[int] = None


class RetryDepth(NamedTuple):
    """One parsed surreal-commands retry line."""

    attempt: int
    next_attempt: int
    wait_seconds: float
    error_type: str
    error_message: str


def parse_retry_message(text: str) -> Optional[RetryDepth]:
    """Parse a surreal-commands retry log line, or ``None`` if it is not one."""
    match = _RETRY_RE.match(text)
    if match is None:
        return None
    return RetryDepth(
        attempt=int(match.group(1)),
        next_attempt=int(match.group(5)),
        wait_seconds=float(match.group(4)),
        error_type=match.group(2).strip(),
        error_message=match.group(3),
    )


def build_retry_visibility_filter(step: int) -> Callable[[Any], bool]:
    """Return a loguru filter that passes only milestone retry records.

    The filter tracks the running distribution (attempts seen, deepest attempt)
    and stamps it onto ``record["extra"]`` so the handler's format can surface
    it. Non-retry records and non-milestone attempts return ``False``.
    """
    stats = {"total": 0, "deepest": 0}

    def _filter(record: Any) -> bool:
        depth = parse_retry_message(record.get("message", ""))
        if depth is None:
            return False
        stats["total"] += 1
        stats["deepest"] = max(stats["deepest"], depth.attempt)
        if step <= 0 or depth.attempt % step != 0:
            return False
        extra = record.setdefault("extra", {})
        extra["retry_total"] = stats["total"]
        extra["retry_deepest"] = stats["deepest"]
        extra["retry_next_wait"] = f"{depth.wait_seconds:.1f}"
        return True

    return _filter


def retry_visibility_step_from_env() -> int:
    """Resolve the milestone step from ``OPEN_NOTEBOOK_RETRY_VISIBILITY_STEP``."""
    raw = os.environ.get("OPEN_NOTEBOOK_RETRY_VISIBILITY_STEP")
    if raw is None:
        return DEFAULT_STEP
    value = raw.strip().lower()
    if value in {"off", "none", "false"}:
        return 0
    try:
        return int(value)
    except ValueError:
        logger.warning(
            "retry_visibility: invalid OPEN_NOTEBOOK_RETRY_VISIBILITY_STEP="
            f"{raw!r}; using {DEFAULT_STEP}"
        )
        return DEFAULT_STEP


def install_retry_visibility(
    step: Optional[int] = None, stream: Any = None
) -> Optional[int]:
    """Install the milestone-retry handler once; return its loguru handler id.

    ``step`` overrides the env var (used by tests). Returns ``None`` when the
    handler is disabled (step <= 0). Idempotent.
    """
    global _installed_handler_id
    if _installed_handler_id is not None:
        return _installed_handler_id

    resolved = retry_visibility_step_from_env() if step is None else step
    if resolved <= 0:
        return None

    _installed_handler_id = logger.add(
        stream if stream is not None else sys.stderr,
        level="DEBUG",
        filter=build_retry_visibility_filter(resolved),
        format=(
            "<red><bold>[Retry]</bold></red> deep retry | {message} | "
            "retries={extra[retry_total]} deepest={extra[retry_deepest]} "
            "next_wait={extra[retry_next_wait]}s\n"
        ),
    )
    return _installed_handler_id


def uninstall_retry_visibility() -> None:
    """Remove the handler (tests / graceful shutdown)."""
    global _installed_handler_id
    if _installed_handler_id is None:
        return
    try:
        logger.remove(_installed_handler_id)
    except ValueError:
        pass
    _installed_handler_id = None
