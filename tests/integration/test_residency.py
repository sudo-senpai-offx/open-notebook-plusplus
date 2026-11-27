"""Tier-1 residency tests for Week 5 Day 3 (Android 4 GB LMK safeguard).

These tests exercise the dispatcher's Ollama hold/admit/reload lifecycle using
``MockResidency`` — no live Ollama binary required. Each test drives
``dispatch_once`` directly with a mocked ``repo_query`` and a mocked
``evaluate`` (always admits), so the only thing holding a job back is the
Ollama residency gate.

Covered:
  1. Heavy job held while chat is active and Ollama is loaded.
  2. Ollama unloaded before heavy job admitted once chat is idle (>= 5 min).
  3. Admission aborted when unload fails (LMK protection).
  4. Ollama reloaded once the heavy/deferred queue drains.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from open_notebook.scheduler import dispatcher as disp
from open_notebook.scheduler.gate import GateDecision
from open_notebook.scheduler.memory import MemoryReading
from open_notebook.scheduler.residency import MockResidency


# ── Helpers ──────────────────────────────────────────────────────────────


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _make_job(job_id: str, lane: str = "heavy", name: str = "process_source") -> dict:
    return {
        "id": f"command:{job_id}",
        "name": name,
        "args": {},
        "lane": lane,
        "created": _now_iso(),
    }


def _fake_repo(held_rows: list[dict], live_rows: list[dict]):
    """Fake repo_query: returns held/live for SELECTs, acknowledges UPDATEs."""
    calls: list[tuple[str, dict]] = []

    async def fake(query: str, vars: dict | None = None) -> list[dict]:
        calls.append((str(query), vars or {}))
        q = str(query)
        if "WHERE status = 'held'" in q:
            return list(held_rows)
        if "WHERE status IN" in q:
            return list(live_rows)
        # UPDATE (promote / hold) — acknowledge and echo the id back.
        return [{"id": (vars or {}).get("id")}]

    return fake, calls


def _setup(
    monkeypatch,
    held_rows: list[dict],
    live_rows: list[dict],
    residency: MockResidency,
    chat_idle: bool,
) -> list[tuple[str, dict]]:
    """Patch every dispatcher dependency except the Ollama residency gate.

    Returns the repo call list so callers can inspect UPDATEs.
    """
    fake_repo, calls = _fake_repo(held_rows, live_rows)

    # repo_query (imported locally inside dispatch_once and by _fetch_jobs)
    monkeypatch.setattr(
        "open_notebook.database.repository.repo_query", fake_repo
    )

    # Termux supervisor client — disabled so supervisor code paths are skipped.
    fake_residency = SimpleNamespace(
        enabled=lambda: False,
        policies=AsyncMock(return_value=None),
        cpu_budget=AsyncMock(return_value=None),
        roles_for_models=lambda keys: [],
    )
    monkeypatch.setattr("open_notebook.scheduler.dispatcher.residency", fake_residency)

    # Plenty of memory so the memory gate never holds.
    monkeypatch.setattr(
        "open_notebook.scheduler.memory.current_memory",
        lambda: MemoryReading(
            readable=True, total=8_000_000_000, available=6_000_000_000, swapped=0
        ),
    )

    # evaluate always admits (the Ollama gate is the only thing holding).
    monkeypatch.setattr(
        "open_notebook.scheduler.gate.evaluate",
        lambda **kw: GateDecision(admit=True, reason="", incremental=0, projected=0),
    )

    # _record_hold is the real hold path — let it run against the fake repo.
    # _reap_stale_jobs / _maybe_ttl_sweep / _maybe_write_heartbeat are no-ops.
    monkeypatch.setattr(
        "open_notebook.scheduler.dispatcher._reap_stale_jobs", AsyncMock()
    )
    monkeypatch.setattr(
        "open_notebook.scheduler.dispatcher._maybe_ttl_sweep", AsyncMock()
    )
    monkeypatch.setattr(
        "open_notebook.scheduler.dispatcher._maybe_write_heartbeat", AsyncMock()
    )

    # Swap in the MockResidency and set chat activity state.
    # NOTE: _ollama_unloaded_by_us is intentionally NOT reset here — test 4's
    # pass 2 needs it to persist True from pass 1. Callers set it explicitly.
    disp._ollama_residency = residency
    disp._batch_force_now = False
    if chat_idle:
        disp._last_chat_activity_at = None  # never active => idle
    else:
        disp._last_chat_activity_at = datetime.now(timezone.utc)  # just now

    return calls


async def _run_dispatch(held_rows: list[dict], live_rows: list[dict]) -> disp.DispatchOutcome:
    return await disp.dispatch_once(generation_bytes=1_000_000, lookup_generation=False)


# ── Test 1: heavy job held while chat active and Ollama loaded ────────────


@pytest.mark.asyncio
async def test_heavy_job_held_while_chat_active_and_ollama_loaded(
    monkeypatch,
):
    """Heavy job stays held when chat is active (< 5 min idle) and Ollama
    is loaded. unload() must NOT be called."""
    held = [_make_job("job-heavy-1", "heavy")]
    live: list[dict] = []
    residency = MockResidency(loaded=True)

    _setup(monkeypatch, held, live, residency, chat_idle=False)
    disp._ollama_unloaded_by_us = False

    outcome = await _run_dispatch(held, live)

    # Job promoted? No — held because Ollama is loaded and chat is active.
    assert "job-heavy-1" not in outcome.promoted, outcome.promoted
    assert outcome.held_reasons, outcome.held_reasons
    # unload() must NOT be called while chat is active.
    assert residency.unload_calls == 0, residency.unload_calls
    assert residency.reload_calls == 0, residency.reload_calls


# ── Test 2: unload Ollama before heavy admission once chat is idle ───────


@pytest.mark.asyncio
async def test_batch_mode_unloads_ollama_before_heavy_admission(
    monkeypatch,
):
    """Once chat is idle (>= 5 min), unload() is called before the heavy job
    transitions held -> running."""
    held = [_make_job("job-heavy-2", "heavy")]
    live: list[dict] = []
    residency = MockResidency(loaded=True)

    _setup(monkeypatch, held, live, residency, chat_idle=True)
    disp._ollama_unloaded_by_us = False

    outcome = await _run_dispatch(held, live)

    # unload() called exactly once before admitting.
    assert residency.unload_calls == 1, residency.unload_calls
    # Job admitted after unload succeeded.
    assert "command:job-heavy-2" in outcome.promoted, outcome.promoted
    assert not outcome.held_reasons, outcome.held_reasons
    assert residency.reload_calls == 0, residency.reload_calls  # not drained yet


# ── Test 3: abort admission if unload fails ───────────────────────────────


@pytest.mark.asyncio
async def test_batch_mode_aborts_admission_if_unload_fails(monkeypatch):
    """If unload() fails or times out, the heavy job remains held (LMK
    protection — never admit against a loaded 2.5 GB model)."""
    held = [_make_job("job-heavy-3", "heavy")]
    live: list[dict] = []
    residency = MockResidency(loaded=True, fail_unload=True)

    _setup(monkeypatch, held, live, residency, chat_idle=True)
    disp._ollama_unloaded_by_us = False

    outcome = await _run_dispatch(held, live)

    # unload attempted but failed.
    assert residency.unload_calls == 1, residency.unload_calls
    # Job NOT admitted — still held.
    assert "job-heavy-3" not in outcome.promoted, outcome.promoted
    assert outcome.held_reasons, outcome.held_reasons
    assert any("unload" in r for r in outcome.held_reasons), outcome.held_reasons


# ── Test 4: reload Ollama once the heavy queue drains ─────────────────────


@pytest.mark.asyncio
async def test_batch_mode_reloads_ollama_after_heavy_queue_drains(
    monkeypatch,
):
    """reload() is called once the last heavy job finishes (queue drained)."""
    residency = MockResidency(loaded=True)

    # Pass 1: one heavy job held, chat idle → unload then admit.
    held_pass1 = [_make_job("job-heavy-4", "heavy")]
    live_pass1: list[dict] = []
    _setup(monkeypatch, held_pass1, live_pass1, residency, chat_idle=True)
    disp._ollama_unloaded_by_us = False
    outcome1 = await _run_dispatch(held_pass1, live_pass1)

    assert residency.unload_calls == 1, residency.unload_calls
    assert "command:job-heavy-4" in outcome1.promoted, outcome1.promoted
    assert disp._ollama_unloaded_by_us is True

    # Pass 2: queue drained (nothing held, nothing running) → reload.
    # NOTE: do NOT reset _ollama_unloaded_by_us here — pass 1 set it True and
    # pass 2 must see it to trigger reload.
    held_pass2: list[dict] = []
    live_pass2: list[dict] = []
    _setup(monkeypatch, held_pass2, live_pass2, residency, chat_idle=True)
    outcome2 = await _run_dispatch(held_pass2, live_pass2)

    assert residency.reload_calls == 1, residency.reload_calls
    assert disp._ollama_unloaded_by_us is False
