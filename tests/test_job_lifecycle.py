"""Job lifecycle (B3) tests."""

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from api import command_service, job_monitor
from open_notebook.domain import notebook as notebook_domain
from open_notebook.utils import job_control


def test_report_progress_merges_and_failopen(monkeypatch):
    calls = []

    async def fake_query(query_str, vars=None):
        calls.append((query_str, vars))
        return []

    monkeypatch.setattr(job_control, "repo_query", fake_query)
    asyncio.run(job_control.report_progress("cmd1", progress=5, stage="starting"))
    assert calls
    q, v = calls[0]
    assert "MERGE" in q

    # fail-open
    async def raising(*a, **k):
        raise Exception("boom")

    monkeypatch.setattr(job_control, "repo_query", raising)
    asyncio.run(job_control.report_progress("cmd2", 10, "x"))  # should not raise


def test_raise_if_cancelled(monkeypatch):
    async def fake_query(query_str, vars=None):
        return [{"cancel_requested": True}]

    monkeypatch.setattr(job_control, "repo_query", fake_query)
    with pytest.raises(job_control.JobCancelledError):
        asyncio.run(job_control.raise_if_cancelled("cmd1"))
    # no-op if None
    asyncio.run(job_control.raise_if_cancelled(None))

    # false
    async def falseq(*a, **k):
        return [{"cancel_requested": False}]

    monkeypatch.setattr(job_control, "repo_query", falseq)
    asyncio.run(job_control.raise_if_cancelled("cmd2"))

    # fail-open
    async def err(*a, **k):
        raise Exception("e")

    monkeypatch.setattr(job_control, "repo_query", err)
    asyncio.run(job_control.raise_if_cancelled("cmd3"))


def test_get_processing_progress_surfaces_progress(monkeypatch):
    async def fake_query(query_str, vars=None):
        return [
            {
                "status": "running",
                "progress": 42,
                "stage": "extracting",
                "heartbeat_at": "2026-10-08T00:00:00+00:00",
                "result": {},
            }
        ]

    monkeypatch.setattr(notebook_domain, "repo_query", fake_query)
    from open_notebook.domain.notebook import Source

    src = Source(title="t", topics=[], command="command:abc")
    res = asyncio.run(src.get_processing_progress())
    assert res is not None
    assert res["progress"] == 42
    assert res["stage"] == "extracting"


def test_command_service_cancel_behaviour(monkeypatch):
    calls = []

    async def fake_query(query_str, vars=None):
        calls.append((query_str, vars))
        return [{"status": "running"}]

    monkeypatch.setattr(command_service, "repo_query", fake_query)
    ok = asyncio.run(command_service.CommandService.cancel_command_job("abc"))
    assert ok is True
    # completed -> False
    calls.clear()

    async def falseq2(query_str, vars=None):
        calls.append((query_str, vars))
        return [{"status": "completed"}]

    monkeypatch.setattr(command_service, "repo_query", falseq2)
    ok = asyncio.run(command_service.CommandService.cancel_command_job("abc"))
    assert ok is False
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_job_monitor_stale_two_pass_and_departure(monkeypatch):
    now = datetime.now(timezone.utc)
    old = (now - timedelta(seconds=400)).isoformat()
    m = job_monitor.JobMonitor(notify=AsyncMock(), interval=0.01, stale_threshold=300)

    # pass 1: fetch_active returns stale running
    async def fetch_active1():
        return [{"id": "command:1", "status": "running", "heartbeat_at": old}]

    async def fetch_one1(job_id):
        return {"id": "command:1", "status": "running", "heartbeat_at": old}

    m._fetch_active = fetch_active1  # type: ignore
    m._fetch_one = fetch_one1  # type: ignore
    marked = []

    async def mark_stale(job_id):
        marked.append(job_id)

    m._mark_stale = mark_stale  # type: ignore
    await m.run_once(now=now)
    assert marked == []  # first pass only candidates
    # pass 2: still stale -> confirm
    await m.run_once(now=now)
    assert marked == ["command:1"]

    # pass 3: departs -> emit
    async def fetch_active3():
        return []

    async def fetch_one3(job_id):
        return {
            "id": "command:1",
            "status": "failed",
            "error_message": "stale: ...",
            "app": "open_notebook",
            "name": "process_source",
            "args": {},
        }

    m._fetch_active = fetch_active3  # type: ignore
    m._fetch_one = fetch_one3  # type: ignore
    m._notify = AsyncMock()  # type: ignore
    await m.run_once(now=now)
    m._notify.assert_called_once()


def test_heartbeat_uses_injectable_tick():
    """Heartbeat thread must call the injected tick and stop cleanly."""
    import threading

    fired = threading.Event()

    async def tick():
        fired.set()

    hb = job_control.Heartbeat("command:hbtest", interval=1, tick=tick)
    hb.start()
    try:
        assert fired.wait(timeout=3), "heartbeat tick never fired"
    finally:
        hb.stop()
    assert not hb._thread or not hb._thread.is_alive()


def test_heartbeat_noop_without_command_id():
    """No thread is started when command_id is falsy."""

    async def tick():
        raise AssertionError("tick must not run without command_id")

    hb = job_control.Heartbeat(None, interval=1, tick=tick)
    hb.start()
    hb.stop()  # must be safe no-ops
    assert hb._thread is None


def test_contextvar_set_get_reset():
    token = job_control.set_current_command_id("command:ctx")
    try:
        assert job_control.get_current_command_id() == "command:ctx"
    finally:
        job_control.reset_current_command_id(token)
    assert job_control.get_current_command_id() is None


def test_cancel_endpoint_returns_flag(monkeypatch):
    """DELETE /api/commands/jobs/{id} delegates to CommandService.cancel."""
    from unittest.mock import patch

    from fastapi.testclient import TestClient

    from api.main import app

    client = TestClient(app, raise_server_exceptions=False)
    with patch(
        "api.routers.commands.CommandService.cancel_command_job",
        new_callable=AsyncMock,
    ) as m:
        m.return_value = True
        resp = client.delete("/api/commands/jobs/command:abc")
        assert resp.status_code == 200
        assert resp.json() == {"job_id": "command:abc", "cancelled": True}
        m.assert_awaited_once_with("command:abc")


def test_process_source_stops_heartbeat_on_failure(monkeypatch):
    """Regression: heartbeat thread must be stopped on every exit path.

    The earlier edit churn dropped the command's `finally: hb.stop()`. An
    unstopped heartbeat keeps writing heartbeat_at on a finished command
    forever (one leaked daemon thread + DB write loop per ingest).
    """
    from types import SimpleNamespace

    import commands.source_commands as sc

    started = []
    stopped = []

    class FakeHeartbeat:
        def __init__(self, command_id, **kwargs):
            self.command_id = command_id

        def start(self):
            started.append(self)

        def stop(self):
            stopped.append(self)

    class FakeSource:
        id = "source:x"
        command = None

        async def save(self):
            return self

    async def failing_ainvoke(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(sc, "Heartbeat", FakeHeartbeat)
    monkeypatch.setattr(sc.Source, "get", AsyncMock(return_value=FakeSource()))
    monkeypatch.setattr(sc, "source_graph", SimpleNamespace(ainvoke=failing_ainvoke))

    input_data = sc.SourceProcessingInput(
        source_id="source:x",
        content_state={"content": "hi"},
        notebook_ids=[],
        transformations=[],
        embed=False,
    )

    with pytest.raises(RuntimeError):
        asyncio.run(sc.process_source_command(input_data))

    assert len(started) == 1
    assert len(stopped) == 1
