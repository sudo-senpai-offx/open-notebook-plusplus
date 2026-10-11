"""P5 lease: an open focus session vetoes hibernate like a leased job."""

from __future__ import annotations

import pytest

from api.governor import evaluate
from open_notebook.domain.focus import FocusSession
from tests.test_governor import NOW


def test_model_defaults_and_validation():
    FocusSession.clear_instance() if hasattr(FocusSession, "clear_instance") else None
    s = FocusSession(status="running", preset="pomodoro", planned_seconds=1500)
    assert s.notebook_id is None
    assert s.ended_at is None
    with pytest.raises(Exception):
        FocusSession(status="napping", preset="pomodoro", planned_seconds=1500)


def test_open_session_vetoes_hibernate():
    decision = evaluate(enabled=True, idle_seconds=600, rows=[], now=NOW, focus_open=2)
    assert decision["action"] == "vetoed"
    assert "focus" in decision["reason"]


def test_closed_sessions_do_not_veto():
    decision = evaluate(enabled=True, idle_seconds=600, rows=[], now=NOW, focus_open=0)
    assert decision["action"] == "would_hibernate"
