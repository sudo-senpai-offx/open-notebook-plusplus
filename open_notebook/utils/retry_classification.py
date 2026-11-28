"""Retry classification: transient vs permanent with exponential backoff.

Classifies extraction/pipeline errors so that only transient failures are
retried, with exponential backoff + jitter. Permanent failures (bad input,
corrupt files, auth errors) fail fast without wasting cycles.

Transient errors: network timeouts, 429/502/503/504, connection resets,
temporary resource exhaustion.
Permanent errors: 400/401/403/404/422, invalid format, corrupt files,
missing language for short audio, unsupported content types.
"""

from __future__ import annotations

import random
from typing import Optional

# HTTP status codes that are safe to retry (transient).
TRANSIENT_STATUS_CODES = frozenset({408, 429, 500, 502, 503, 504})

# HTTP status codes that should never be retried (permanent).
PERMANENT_STATUS_CODES = frozenset({400, 401, 403, 404, 422})

# Error substrings that indicate transient failures (checked in message text).
TRANSIENT_ERROR_MARKERS = frozenset({
    "timeout",
    "timed out",
    "connection reset",
    "connection refused",
    "network is unreachable",
    "temporary failure",
    "service unavailable",
    "throttl",
    "rate limit",
    "overloaded",
    "try again",
})

# Error substrings that indicate permanent failures.
PERMANENT_ERROR_MARKERS = frozenset({
    "invalid",
    "corrupt",
    "corrupted",
    "unsupported",
    "not found",
    "unauthorized",
    "forbidden",
    "bad request",
    "unprocessable",
    "language_required",
    "no usable model",
    "missing language",
})


def is_transient_error(exc: BaseException) -> bool:
    """Return True if ``exc`` is a transient error worth retrying.

    Classification order:
      1. HTTP status codes (if the exception carries one)
      2. Error message substring matching
      3. Unknown → not transient (fail safe: don't retry blindly)
    """
    msg = str(exc).lower()

    # Check for transient status codes embedded in the message.
    for code in TRANSIENT_STATUS_CODES:
        if str(code) in msg:
            return True

    # Check for transient error markers.
    for marker in TRANSIENT_ERROR_MARKERS:
        if marker in msg:
            return True

    return False


def is_permanent_error(exc: BaseException) -> bool:
    """Return True if ``exc`` is a permanent error that should not be retried."""
    msg = str(exc).lower()

    # Check for permanent status codes embedded in the message.
    for code in PERMANENT_STATUS_CODES:
        if str(code) in msg:
            return True

    # Check for permanent error markers.
    for marker in PERMANENT_ERROR_MARKERS:
        if marker in msg:
            return True

    return False


def classify_error(exc: BaseException) -> str:
    """Classify an error as ``"transient"``, ``"permanent"``, or ``"unknown"``.

    Unknown errors are treated as non-transient (fail safe) — the caller
    decides whether to retry based on its own policy.
    """
    if is_transient_error(exc):
        return "transient"
    if is_permanent_error(exc):
        return "permanent"
    return "unknown"


def retry_backoff_seconds(attempt: int, base: float = 1.0, cap: float = 60.0) -> float:
    """Exponential backoff with jitter for retry attempt ``attempt`` (1-indexed).

    Formula: min(cap, base * 2^(attempt-1)) + uniform(0, 1)
    """
    return min(cap, base * (2.0 ** max(0, attempt - 1)) + random.uniform(0, 1.0))


def should_retry(
    exc: BaseException,
    attempt: int,
    max_attempts: int = 3,
    retry_unknown: bool = False,
) -> tuple[bool, Optional[float]]:
    """Decide whether to retry and, if so, after how many seconds.

    Returns ``(should_retry, delay_seconds_or_None)``.

    Rules:
      - attempt >= max_attempts → never retry
      - permanent → never retry
      - transient → retry with backoff
      - unknown → retry only if ``retry_unknown=True``
    """
    if attempt >= max_attempts:
        return False, None

    classification = classify_error(exc)

    if classification == "permanent":
        return False, None

    if classification == "transient":
        return True, retry_backoff_seconds(attempt)

    # Unknown: caller opt-in
    if retry_unknown:
        return True, retry_backoff_seconds(attempt)

    return False, None
