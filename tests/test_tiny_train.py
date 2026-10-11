"""T5-a1: tiny-trainer core. Gated entry, lazy torch, capped runs.

Denied gate raises InvalidInputError (permanent: worker stop_on lists it,
no pointless retries). Smoke run is seconds on synthetic text.
"""

from __future__ import annotations

import math

import pytest

from open_notebook.exceptions import InvalidInputError


def _allow_tiny(monkeypatch):
    monkeypatch.setenv("OPEN_NOTEBOOK_ALLOW_TINY_TRAIN", "true")
    from open_notebook.utils import capability_tier

    monkeypatch.setattr(capability_tier, "mem_available_mb", lambda: 4096)
    monkeypatch.setattr(capability_tier, "cpu_count", lambda: 4)


def test_denied_gate_is_permanent(monkeypatch):
    monkeypatch.delenv("OPEN_NOTEBOOK_ALLOW_TINY_TRAIN", raising=False)
    from open_notebook.training.tiny import run_tiny_train

    with pytest.raises(InvalidInputError, match="tiny_train"):
        run_tiny_train("hello world " * 50, max_steps=2)


def test_smoke_run_learns_and_caps(monkeypatch):
    _allow_tiny(monkeypatch)
    from open_notebook.training.tiny import run_tiny_train

    seen = []
    result = run_tiny_train(
        ("hello world, tiny model. " * 40),
        max_steps=6,
        max_seconds=300,
        progress=lambda step, loss: seen.append((step, loss)),
    )
    assert result["steps"] == 6
    assert math.isfinite(result["loss"])
    assert result["params"] < 20_000_000
    assert [s for s, _ in seen] == [1, 2, 3, 4, 5, 6]


def test_time_budget_stops_early(monkeypatch):
    _allow_tiny(monkeypatch)
    from open_notebook.training.tiny import run_tiny_train

    result = run_tiny_train(("abcde " * 200), max_steps=10_000, max_seconds=0)
    assert result["steps"] < 10_000
