"""T5 worker command: text in, stats out, nothing durable.

Denied gates are permanent (no retry churn); isolation failures are
transient (worker may retry). Heartbeats bracket the isolated run.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from commands.training_commands import TrainTinyInput, train_tiny_command
from open_notebook.exceptions import InvalidInputError


def _input(**overrides):
    args = {"text": "hello worker " * 50, "name": "smoke", "max_steps": 2}
    args.update(overrides)
    return TrainTinyInput(**args)


@pytest.mark.asyncio
async def test_success_returns_stats():
    stats = {"loss": 2.5, "steps": 2, "params": 800_000, "wall_s": 6.0}
    with patch(
        "commands.training_commands.run_tiny_train_isolated",
        new=AsyncMock(return_value=stats),
    ):
        out = await train_tiny_command(_input())
    assert out.success is True
    assert out.loss == 2.5
    assert out.steps == 2
    assert out.error_message is None


@pytest.mark.asyncio
async def test_denied_gate_raises_permanent():
    with patch(
        "commands.training_commands.run_tiny_train_isolated",
        new=AsyncMock(side_effect=InvalidInputError("denied")),
    ):
        with pytest.raises(InvalidInputError):
            await train_tiny_command(_input())


@pytest.mark.asyncio
async def test_child_failure_propagates_transient():
    with patch(
        "commands.training_commands.run_tiny_train_isolated",
        new=AsyncMock(side_effect=RuntimeError("boom")),
    ):
        with pytest.raises(RuntimeError):
            await train_tiny_command(_input())
