"""B2: deep retries must surface as a visible retry-distribution line.

The worker runs loguru at INFO and ``process_source`` logs every retry at
DEBUG, so without this seam a command stuck at attempt 12/15 is invisible.
"""

import io
from typing import Any, Dict

import pytest
from loguru import logger

from open_notebook.utils.retry_visibility import (
    DEFAULT_STEP,
    build_retry_visibility_filter,
    install_retry_visibility,
    parse_retry_message,
    retry_visibility_step_from_env,
    uninstall_retry_visibility,
)

NON_MILESTONE = (
    "[Retry] Attempt 3 failed with DatabaseOperationError: boom, "
    "waiting 2.0s before retry 4"
)
MILESTONE = (
    "[Retry] Attempt 5 failed with DatabaseOperationError: boom, "
    "waiting 8.0s before retry 6"
)


@pytest.fixture(autouse=True)
def _clean_handler():
    uninstall_retry_visibility()
    yield
    uninstall_retry_visibility()


def test_parse_retry_message_basic():
    depth = parse_retry_message(MILESTONE)
    assert depth is not None
    assert depth.attempt == 5
    assert depth.next_attempt == 6
    assert depth.wait_seconds == 8.0
    assert depth.error_type == "DatabaseOperationError"
    assert depth.error_message == "boom"


def test_parse_retry_message_ignores_non_retry_lines():
    assert (
        parse_retry_message("Executing command: open_notebook.process_source") is None
    )
    assert parse_retry_message("[Retry] Attempt x failed") is None


def test_parse_retry_message_uses_last_waiting_clause():
    msg = (
        "[Retry] Attempt 2 failed with Error: inner, waiting 1.0s before retry 3, "
        "waiting 4.0s before retry 4"
    )
    depth = parse_retry_message(msg)
    assert depth is not None
    assert depth.error_message == "inner, waiting 1.0s before retry 3"
    assert depth.wait_seconds == 4.0
    assert depth.next_attempt == 4


def test_filter_passes_only_milestones_and_tracks_distribution():
    record: Dict[str, Any] = {"message": "", "extra": {}}
    retry_filter = build_retry_visibility_filter(step=5)

    record["message"] = NON_MILESTONE
    assert retry_filter(record) is False
    assert record["extra"] == {}

    record["message"] = MILESTONE
    assert retry_filter(record) is True
    assert record["extra"]["retry_total"] == 2
    assert record["extra"]["retry_deepest"] == 5
    assert record["extra"]["retry_next_wait"] == "8.0"


def test_filter_disabled_when_step_is_zero():
    record: Dict[str, Any] = {"message": MILESTONE, "extra": {}}
    assert build_retry_visibility_filter(step=0)(record) is False
    assert record["extra"] == {}


@pytest.mark.parametrize(
    "raw,expected",
    [
        (None, DEFAULT_STEP),
        ("5", 5),
        ("10", 10),
        ("off", 0),
        ("none", 0),
        ("0", 0),
    ],
)
def test_step_from_env(monkeypatch, raw, expected):
    if raw is None:
        monkeypatch.delenv("OPEN_NOTEBOOK_RETRY_VISIBILITY_STEP", raising=False)
    else:
        monkeypatch.setenv("OPEN_NOTEBOOK_RETRY_VISIBILITY_STEP", raw)
    assert retry_visibility_step_from_env() == expected


def test_step_from_env_invalid_falls_back(monkeypatch):
    monkeypatch.setenv("OPEN_NOTEBOOK_RETRY_VISIBILITY_STEP", "banana")
    assert retry_visibility_step_from_env() == DEFAULT_STEP


def test_install_emits_milestone_line_and_is_idempotent():
    stream = io.StringIO()
    handler_id = install_retry_visibility(step=5, stream=stream)
    assert handler_id is not None
    # Second install must not add another handler.
    assert install_retry_visibility(step=5, stream=stream) == handler_id

    logger.debug(NON_MILESTONE)
    assert stream.getvalue() == ""

    logger.debug(MILESTONE)
    out = stream.getvalue()
    assert "deep retry" in out
    assert "retries=2" in out
    assert "deepest=5" in out
    assert "next_wait=8.0" in out


def test_install_disabled_by_zero_step_returns_none():
    assert install_retry_visibility(step=0, stream=io.StringIO()) is None
