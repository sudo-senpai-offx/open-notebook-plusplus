"""T5-a2: isolated training keeps a crash/OOM inside the child.

Parent never trains in-process; the heartbeat thread survives the child.
Timeouts propagate raw (transient -> worker retries); gate denials fail
fast without spawning.
"""

from __future__ import annotations

import math

import pytest

from open_notebook.exceptions import InvalidInputError
from open_notebook.training.tiny import run_tiny_train_isolated


def _allow_tiny(monkeypatch):
    monkeypatch.setenv("OPEN_NOTEBOOK_ALLOW_TINY_TRAIN", "true")
    from open_notebook.utils import capability_tier

    monkeypatch.setattr(capability_tier, "mem_available_mb", lambda: 4096)
    monkeypatch.setattr(capability_tier, "cpu_count", lambda: 4)


@pytest.mark.asyncio
async def test_isolated_run_returns_result(monkeypatch):
    _allow_tiny(monkeypatch)
    result = await run_tiny_train_isolated(
        ("hello isolated world. " * 40), max_steps=2, max_seconds=300
    )
    assert result["steps"] == 2
    assert math.isfinite(result["loss"])


@pytest.mark.asyncio
async def test_isolated_run_denied_without_spawning(monkeypatch):
    monkeypatch.delenv("OPEN_NOTEBOOK_ALLOW_TINY_TRAIN", raising=False)
    with pytest.raises(InvalidInputError, match="tiny_train"):
        await run_tiny_train_isolated("hello " * 50)


@pytest.mark.asyncio
async def test_child_failure_surfaced_not_swallowed(monkeypatch):
    _allow_tiny(monkeypatch)
    with pytest.raises(RuntimeError, match="tiny worker failed"):
        await run_tiny_train_isolated("too short", max_steps=2)
