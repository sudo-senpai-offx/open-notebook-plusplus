"""Governor sleep/wake actions (worker only; media + full order deferred).

Sleep needs all three: policy enabled, lease-guard clear, would_hibernate.
Draining is set before the stop and rolled back if the stop fails, so the
flag never claims a sleep that did not happen.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from api import governor as governor_module
from api.governor import Governor
from tests.test_governor import NOW, _row


class FakeRunner:
    def __init__(self, fail_stop=False):
        self.actions = []
        self.fail_stop = fail_stop

    async def stop(self, program="worker"):
        self.actions.append(("stop", program))
        if self.fail_stop:
            raise RuntimeError("supervisor unreachable")

    async def start(self, program="worker"):
        self.actions.append(("start", program))


def _idle_rows():
    return [_row("completed", 3600, hb_ago_s=3600)]


def _stub_service_state(monkeypatch, draining=False):
    state = SimpleNamespace(draining=draining, set_draining=AsyncMock())
    monkeypatch.setattr(
        governor_module.ServiceState,
        "get_instance",
        classmethod(lambda cls: _ready(state)),
    )
    return state


async def _ready(value):
    return value


def _enable(monkeypatch):
    monkeypatch.setenv("OPEN_NOTEBOOK_HIBERNATE_ENABLE", "true")


@pytest.mark.asyncio
async def test_sleep_sets_draining_and_stops_worker(monkeypatch):
    async def fake_query(*args, **kwargs):
        return _idle_rows()

    monkeypatch.setattr(governor_module, "repo_query", fake_query)
    _enable(monkeypatch)
    state = _stub_service_state(monkeypatch)
    runner = FakeRunner()
    gov = Governor(runner=runner)
    decision = await gov.run_once(now=NOW)
    assert decision["action"] == "would_hibernate"
    assert ("stop", "worker") in runner.actions
    state.set_draining.assert_awaited_once_with(True)


@pytest.mark.asyncio
async def test_veto_never_stops(monkeypatch):
    async def fake_query(*args, **kwargs):
        return [_row("running", 10)]

    monkeypatch.setattr(governor_module, "repo_query", fake_query)
    _enable(monkeypatch)
    _stub_service_state(monkeypatch)
    runner = FakeRunner()
    gov = Governor(runner=runner)
    decision = await gov.run_once(now=NOW)
    assert decision["action"] == "vetoed"
    assert runner.actions == []


@pytest.mark.asyncio
async def test_failed_stop_rolls_back_draining(monkeypatch):
    async def fake_query(*args, **kwargs):
        return _idle_rows()

    monkeypatch.setattr(governor_module, "repo_query", fake_query)
    _enable(monkeypatch)
    state = _stub_service_state(monkeypatch)
    runner = FakeRunner(fail_stop=True)
    gov = Governor(runner=runner)
    with pytest.raises(RuntimeError, match="supervisor unreachable"):
        await gov.run_once(now=NOW)
    calls = [c.args for c in state.set_draining.await_args_list]
    assert (True,) in calls and calls[-1] == (False,)
    assert gov._asleep is False


@pytest.mark.asyncio
async def test_no_double_stop_when_already_asleep(monkeypatch):
    async def fake_query(*args, **kwargs):
        return _idle_rows()

    monkeypatch.setattr(governor_module, "repo_query", fake_query)
    _enable(monkeypatch)
    _stub_service_state(monkeypatch)
    runner = FakeRunner()
    gov = Governor(runner=runner)
    gov._asleep = True
    await gov.run_once(now=NOW)
    assert runner.actions == []


@pytest.mark.asyncio
async def test_wake_restarts_and_clears(monkeypatch):
    state = _stub_service_state(monkeypatch)
    runner = FakeRunner()
    gov = Governor(runner=runner)
    gov._asleep = True
    await gov.wake()
    assert ("start", "worker") in runner.actions
    state.set_draining.assert_awaited_once_with(False)
    assert gov._asleep is False
