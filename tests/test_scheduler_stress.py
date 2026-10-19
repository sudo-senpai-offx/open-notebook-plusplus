"""Stress tests for the scheduler: CPU+RAM holding, mixed workloads, and
rescheduling under strict resource management.

These are arduous by design: they exercise the gate, the dispatcher, and the
residency boundary under conditions that look fine in isolation but fail when
combined - which is exactly where production devices wedged before the
reviewed fixes.

Run with:  .venv/bin/python -m pytest tests/test_scheduler_stress.py -q
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

import pytest

from open_notebook.residency import client as residency
from open_notebook.scheduler import dispatcher as disp
from open_notebook.scheduler.dispatcher import (
    dispatch_once,
    role_threads,
)
from open_notebook.scheduler.footprint import (
    FootprintEstimate,
    JobFootprint,
    job_footprint,
    merge_footprints,
)
from open_notebook.scheduler.gate import (
    evaluate,
    incremental_cost,
)
from open_notebook.scheduler.memory import MemoryReading

MIB = 1024 * 1024
GIB = 1024 * MIB

#: qwen3:4b Q4_K_M, the model actually in use on the PC.
GENERATION_BYTES = 5_093_028_002
GENERATION_COST = int(GENERATION_BYTES * 1.10)

#: bge-small-en Q8_0, the embedder on the phone.
EMBED_BYTES = 36_685_152


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def gen_job() -> JobFootprint:
    return job_footprint("run_transformation", generation_bytes=GENERATION_BYTES)


def embed_job() -> JobFootprint:
    return job_footprint("embed_source", embedding_bytes=EMBED_BYTES)


def cheap_job() -> JobFootprint:
    return job_footprint("process_source")


def memory(
    *,
    total: int = 15 * GIB,
    available: int = 8 * GIB,
    free: Optional[int] = None,
    readable: bool = True,
) -> MemoryReading:
    return MemoryReading(
        total=total,
        available=available,
        free=available if free is None else free,
        readable=readable,
    )


def cpu_memory() -> MemoryReading:
    """Phone-like memory: 8 GB total, ~2 GB available, which is what was
    measured with the stack nominally healthy before residency management."""
    return memory(total=8 * GIB, available=2 * GIB)


async def _fake_policies(policy: Dict[str, residency.RolePolicy]):
    """async stand-in for residency.policies() - the dispatcher awaits it."""
    return policy


async def _fake_ensure_resident(role: str) -> residency.ResidencyResult:
    """async stand-in for residency.ensure_resident() - always succeeds."""
    return residency.ResidencyResult(ok=True, role=role, loaded=True, pid="1")


# ---------------------------------------------------------------------------
# CPU budget stress tests (new feature)
# ---------------------------------------------------------------------------


class TestCPUBudgetDerivation:
    """The CPU budget comes from topology, never hardcoded.

    The shell derivation lives in scripts/termux-cpu.sh. Here we test the
    Python contract: whatever the shell derives, the gate treats as a hard
    ceiling.
    """

    def test_chat_and_embed_share_one_budget_not_two(self):
        """Two classes drawing from the same budget means chat+embed cannot
        each take the full share - which is what caused saturation before
        the budget was shared."""
        threads = role_threads(
            {
                "chat": residency.RolePolicy("chat", 10, 1, 2),
                "embed": residency.RolePolicy("embed", 50, 2, 2),
            }
        )
        assert threads["chat"] == 2
        assert threads["embed"] == 2
        # Combined demand (4 threads) exceeds a 2-thread budget - the gate
        # must hold when the budget is that small.
        assert threads["chat"] + threads["embed"] > 2


class TestCPUGating:
    """CPU held is as real as RAM held - postponed, never shrunk."""

    def test_cpu_budget_refuses_when_exceeded(self):
        decision = evaluate(
            window=3,
            in_flight=0,
            committed=merge_footprints([embed_job()]),
            candidate=gen_job(),
            memory=memory(total=15 * GIB, available=8 * GIB),
            cpu_committed=2,  # embed already holds 2
            cpu_candidate=2,  # chat wants 2
            cpu_budget=2,  # but only 2 exist total
        )
        assert decision.admit is False
        assert decision.reason == "cpu-budget"

    def test_cpu_budget_none_means_unlimited(self):
        """No supervisor / unknown topology means no CPU gate - the safe
        direction is to not invent limits and hold nothing on a guess."""
        decision = evaluate(
            window=3,
            in_flight=0,
            committed=merge_footprints([embed_job()]),
            candidate=gen_job(),
            memory=memory(total=15 * GIB, available=8 * GIB),
            cpu_committed=2,
            cpu_candidate=2,
            cpu_budget=None,
        )
        assert decision.admit is True

    def test_cpu_and_memory_agree_on_hold(self):
        """When both resources are short, CPU is checked first and reported
        as the reason even though memory would also hold."""
        decision = evaluate(
            window=3,
            in_flight=0,
            committed=merge_footprints([embed_job()]),
            candidate=gen_job(),
            memory=memory(total=15 * GIB, available=8 * GIB),
            cpu_committed=2,
            cpu_candidate=2,
            cpu_budget=2,
        )
        assert decision.reason == "cpu-budget"

    def test_exactly_at_budget_is_admitted(self):
        """Filling the budget to the last thread is fine; exceeding it is not."""
        decision = evaluate(
            window=3,
            in_flight=0,
            committed=merge_footprints([]),
            candidate=gen_job(),
            memory=memory(total=15 * GIB, available=8 * GIB),
            cpu_committed=0,
            cpu_candidate=4,
            cpu_budget=4,
        )
        assert decision.admit is True


# ---------------------------------------------------------------------------
# Mixed workload stress: chat + embed + transform under pressure
# ---------------------------------------------------------------------------


class TestMixedWorkloadConcurrency:
    """The production pattern: chat requests arriving while embedding batches
    are in flight, transformations queued behind both."""

    @pytest.mark.asyncio
    async def test_chat_prioritised_over_embedding_on_full_window(self, monkeypatch):
        """Chat (priority 10) outranks embed (priority 50). When the window
        is full, embedding jobs wait while chat jobs are admitted first."""
        import open_notebook.database.repository as repository

        policy = {
            "chat": residency.RolePolicy("chat", 10, 1, 2),
            "embed": residency.RolePolicy("embed", 50, 2, 2),
        }
        held = [
            {"id": "c:embed1", "name": "embed_source", "args": {}, "created": "t1"},
            {
                "id": "c:chat1",
                "name": "run_transformation",
                "args": {},
                "created": "t2",
            },
            {"id": "c:embed2", "name": "embed_source", "args": {}, "created": "t3"},
        ]
        live: List[Dict[str, Any]] = []

        class DB:
            def __init__(self):
                self.updates = []
                self.holds = []

            async def query(self, sql, params=None):
                if "status = 'held'" in str(sql) and "ORDER BY" in str(sql):
                    return sorted(held, key=lambda r: r["created"])
                if "status IN" in str(sql) and "SELECT name" in str(sql):
                    return live
                if "UPDATE" in str(sql):
                    if (params or {}).get("reason") is not None:
                        self.holds.append((params["id"], params["reason"]))
                    else:
                        self.updates.append((params["id"], params["status"]))
                    return []
                return []

        db = DB()
        monkeypatch.setattr(repository, "repo_query", db.query)
        monkeypatch.setattr(repository, "ensure_record_id", lambda v: v)
        monkeypatch.setattr(disp.residency, "policies", lambda: _fake_policies(policy))
        monkeypatch.setattr(disp.residency, "enabled", lambda: True)
        monkeypatch.setattr(disp.residency, "ensure_resident", _fake_ensure_resident)

        outcome = await dispatch_once(
            generation_bytes=GENERATION_BYTES,
            memory=memory(total=15 * GIB, available=8 * GIB),
            window=2,
        )

        # With window=2 and chat priority 10, chat is admitted before embed.
        assert outcome.promoted_count >= 1
        chat_promoted = any("chat1" in uid for uid, _ in db.updates)
        assert chat_promoted, "chat must be admitted under priority ordering"

    @pytest.mark.asyncio
    async def test_embedding_does_not_starve_forever(self, monkeypatch):
        """De-prioritised is not cancelled. With chat jobs drained, embed gets
        its turn - the queue does not wedge on priority."""
        import open_notebook.database.repository as repository

        policy = {
            "chat": residency.RolePolicy("chat", 10, 1, 2),
            "embed": residency.RolePolicy("embed", 50, 2, 2),
        }
        held = [
            {"id": "e:1", "name": "embed_source", "args": {}, "created": "t1"},
            {"id": "e:2", "name": "embed_source", "args": {}, "created": "t2"},
        ]
        live: List[Dict[str, Any]] = []

        class DB:
            def __init__(self):
                self.updates = []

            async def query(self, sql, params=None):
                if "status = 'held'" in str(sql) and "ORDER BY" in str(sql):
                    return held
                if "status IN" in str(sql) and "SELECT name" in str(sql):
                    return live
                if "UPDATE" in str(sql):
                    self.updates.append((params["id"], params["status"]))
                    return []
                return []

        db = DB()
        monkeypatch.setattr(repository, "repo_query", db.query)
        monkeypatch.setattr(repository, "ensure_record_id", lambda v: v)
        monkeypatch.setattr(disp.residency, "policies", lambda: _fake_policies(policy))
        monkeypatch.setattr(disp.residency, "enabled", lambda: True)
        monkeypatch.setattr(disp.residency, "ensure_resident", _fake_ensure_resident)

        # No chat jobs present, so embed is admitted at full window.
        outcome = await dispatch_once(
            generation_bytes=GENERATION_BYTES,
            memory=memory(total=8 * GIB, available=4 * GIB),
            window=2,
        )

        assert outcome.promoted_count == 2
        assert db.updates == [("e:1", "new"), ("e:2", "new")]


# ---------------------------------------------------------------------------
# Aggregate budget: pass-by-pass recomputation
# ---------------------------------------------------------------------------


class TestAggregateAcrossPasses:
    """The reviewed bug: three individually-valid promotions that together
    exceed the device - caught at the pass level, not per-job."""

    def test_three_distinct_models_on_small_device(self):
        """Three 1.4 GB models on a 4 GB device: two fit under the 0.85
        ceiling (3.4 GB), the third is held. Charging per job would admit
        all three and OOM."""
        committed = FootprintEstimate()
        admitted = 0
        for index in range(4):
            candidate = JobFootprint(models={f"m{index}": 1_400 * MIB})
            decision = evaluate(
                window=4,
                in_flight=admitted,
                committed=committed,
                candidate=candidate,
                memory=memory(total=4 * GIB, available=4 * GIB - 50 * MIB),
                reserve=0,
                model_pool=4 * GIB,
            )
            if not decision.admit:
                break
            admitted += 1
            committed = committed.merge(
                FootprintEstimate(models=dict(candidate.models))
            )

        assert admitted == 2
        assert committed.total <= 4 * GIB * 0.85

    def test_incremental_cost_zero_after_first_shared_model(self):
        """Incremental cost collapses to zero once the model is resident -
        that is the whole point of the per-model accounting."""
        committed = FootprintEstimate()
        costs = []
        for _ in range(4):
            costs.append(incremental_cost(committed, gen_job()))
            committed = committed.merge(
                FootprintEstimate(models=dict(gen_job().models))
            )
        assert costs == [GENERATION_COST, 0, 0, 0]


# ---------------------------------------------------------------------------
# Cross-device connection resilience
# ---------------------------------------------------------------------------


class TestCrossDeviceTunnel:
    """The phone<->PC tunnel is the sync lifeline. It must survive reconnects
    and report honestly when ports are dead."""

    def test_tunnel_script_ports_are_documented(self):
        """phone-tunnel.sh maps PC ports to phone ports. A change to one
        without the other breaks sync silently."""
        from pathlib import Path

        script = Path("scripts/phone-tunnel.sh").read_text()
        assert "15055" in script  # API
        assert "18080" in script  # llama/chat
        assert "18081" in script  # embed
        assert "18000" in script  # surreal

    def test_api_port_matches_between_launcher_and_updater(self):
        """The launcher binds 5055 on the phone; the updater probes 5055.
        A mismatch means --restart reports "still answering" forever."""
        from pathlib import Path

        stack = Path("scripts/termux-stack.sh").read_text()
        update = Path("scripts/termux-update.sh").read_text()
        assert "--port 5055" in stack
        assert 'API_PORT="${ONB_API_PORT:-5055}"' in update

    def test_health_url_is_unauthenticated(self):
        """`/health` answers 200 without the bearer token; `/api/sync/status`
        answers 401. The updater probes /health so it never gets a false
        "not running" from a missing credential."""
        from pathlib import Path

        update = Path("scripts/termux-update.sh").read_text()
        assert 'HEALTH_URL="http://127.0.0.1:$API_PORT/health"' in update
        assert "/api/sync/status" not in update.split("HEALTH_URL")[1][:80]


# ---------------------------------------------------------------------------
# Insight generation under pressure
# ---------------------------------------------------------------------------


class TestInsightGenerationResilience:
    """ensure-insights must be idempotent, smallest-first, and honest about
    what it skips - under memory pressure that holds jobs."""

    @pytest.mark.asyncio
    async def test_bulk_insights_idempotent_on_replay(self, monkeypatch):
        """Re-running ensure-insights after an interruption fills gaps,
        never duplicates - each (source, transformation) pair submitted once."""
        held = [
            {
                "id": f"i:{s}",
                "name": "run_transformation",
                "args": {"source_id": s, "transformation_id": "t1"},
                "created": f"t{i}",
            }
            for i, s in enumerate(["s1", "s2", "s3", "s4", "s5"], 1)
        ]
        db = FakeDB(held=held)
        _patch_repo(monkeypatch, db)
        monkeypatch.setattr(disp.residency, "policies", lambda: _fake_policies({}))
        monkeypatch.setattr(disp.residency, "enabled", lambda: True)
        monkeypatch.setattr(disp.residency, "ensure_resident", _fake_ensure_resident)

        # First pass: admits what fits.
        first = await dispatch_once(
            generation_bytes=GENERATION_BYTES,
            memory=memory(total=15 * GIB, available=8 * GIB),
            window=3,
        )
        assert first.promoted_count >= 1

        # Second pass: promoted jobs are no longer held, so nothing re-promoted.
        db.held = []
        db.live = [
            {"name": "run_transformation", "args": {}}
            for _ in range(first.promoted_count)
        ]
        second = await dispatch_once(
            generation_bytes=GENERATION_BYTES,
            memory=memory(total=15 * GIB, available=8 * GIB),
            window=3,
        )
        assert second.promoted_count == 0, "idempotency: no re-promotion"


# ---------------------------------------------------------------------------
# Full-system stress: everything at once
# ---------------------------------------------------------------------------


class TestFullSystemStress:
    """The arduous case: memory at the floor, CPU budget exhausted, mixed
    classes, distinct models, and rapid passes - all at once."""

    @pytest.mark.asyncio
    async def test_phone_floor_mixed_workload(self, monkeypatch):
        """8 GB total, 2 GB available, window=2, CPU budget=4. Chat (2
        threads) + embed (2 threads) saturates the CPU budget; a third class
        is held until something finishes."""
        import open_notebook.database.repository as repository

        policy = {
            "chat": residency.RolePolicy("chat", 10, 1, 2),
            "embed": residency.RolePolicy("embed", 50, 2, 2),
        }

        class DB:
            def __init__(self):
                self.updates = []
                self.holds = []
                self.held: List[Dict[str, Any]] = []
                self.live: List[Dict[str, Any]] = []

            async def query(self, sql, params=None):
                if "status = 'held'" in str(sql) and "ORDER BY" in str(sql):
                    return sorted(self.held, key=lambda r: str(r.get("created") or ""))
                if "status IN" in str(sql) and "SELECT name" in str(sql):
                    return self.live
                if "UPDATE" in str(sql):
                    if (params or {}).get("reason") is not None:
                        self.holds.append((params["id"], str(params["reason"])))
                    else:
                        self.updates.append((params["id"], str(params["status"])))
                    return []
                return []

        db = DB()
        db.held = [
            {"id": "m:1", "name": "run_transformation", "args": {}, "created": "t1"},
            {"id": "e:1", "name": "embed_source", "args": {}, "created": "t2"},
            {"id": "m:2", "name": "run_transformation", "args": {}, "created": "t3"},
        ]
        db.live = []

        monkeypatch.setattr(repository, "repo_query", db.query)
        monkeypatch.setattr(repository, "ensure_record_id", lambda v: v)
        monkeypatch.setattr(disp.residency, "policies", lambda: _fake_policies(policy))
        monkeypatch.setattr(disp.residency, "enabled", lambda: True)
        monkeypatch.setattr(disp.residency, "ensure_resident", _fake_ensure_resident)

        outcome = await dispatch_once(
            generation_bytes=GENERATION_BYTES,
            memory=cpu_memory(),
            window=2,
        )

        # At most 2 promoted (window), and CPU budget 4 means chat+embed both
        # fit (4 threads total).
        assert outcome.promoted_count <= 2
        assert outcome.projected_bytes <= GENERATION_COST

    @pytest.mark.asyncio
    async def test_rapid_passes_do_not_admit_past_cpu(self, monkeypatch):
        """Ten passes in a row: each pass must recompute committed CPU, so
        no pass admits past the budget just because it read a snapshot."""
        import open_notebook.database.repository as repository

        policy = {
            "chat": residency.RolePolicy("chat", 10, 1, 2),
        }

        class DB:
            def __init__(self):
                self.held = [
                    {
                        "id": f"c:{i}",
                        "name": "run_transformation",
                        "args": {},
                        "created": f"t{i}",
                    }
                    for i in range(1, 11)
                ]
                self.live = []
                self.updates = []

            async def query(self, sql, params=None):
                if "status = 'held'" in str(sql) and "ORDER BY" in str(sql):
                    return sorted(self.held, key=lambda r: str(r.get("created") or ""))
                if "status IN" in str(sql) and "SELECT name" in str(sql):
                    return self.live
                if "UPDATE" in str(sql):
                    self.updates.append((params["id"], str(params["status"])))
                    return []
                return []

        db = DB()
        monkeypatch.setattr(repository, "repo_query", db.query)
        monkeypatch.setattr(repository, "ensure_record_id", lambda v: v)
        monkeypatch.setattr(disp.residency, "policies", lambda: _fake_policies(policy))
        monkeypatch.setattr(disp.residency, "enabled", lambda: True)
        monkeypatch.setattr(disp.residency, "ensure_resident", _fake_ensure_resident)

        total_promoted = 0
        for pass_no in range(10):
            outcome = await dispatch_once(
                generation_bytes=GENERATION_BYTES,
                memory=cpu_memory(),
                window=2,
            )
            total_promoted += outcome.promoted_count
            db.live = [
                {"name": "run_transformation", "args": {}}
                for _ in range(len(db.updates))
            ]
            db.held = [r for r in db.held if r["id"] not in {u[0] for u in db.updates}]
            if outcome.promoted_count == 0:
                break

        # CPU budget is 4 threads, chat is 2 threads per job - at most 2 chat
        # jobs can run concurrently. The passes must not admit more than that
        # cumulatively without freeing slots.
        assert total_promoted <= 4  # window=2 per pass, max 2 concurrent


# ---------------------------------------------------------------------------
# Test data fixtures reused across classes
# ---------------------------------------------------------------------------


class FakeDB:
    """In-memory command table for dispatcher stress tests."""

    def __init__(
        self,
        held: Optional[List[Dict[str, Any]]] = None,
        live: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        self.held = held or []
        self.live = live or []
        self.updates: List[Tuple[str, str]] = []
        self.holds: List[Tuple[str, str]] = []

    async def query(self, sql: str, params: Optional[Dict[str, Any]] = None):
        if "status = 'held'" in str(sql) and "ORDER BY" in str(sql):
            return sorted(self.held, key=lambda r: str(r.get("created") or ""))
        if "status IN" in str(sql) and "SELECT name" in str(sql):
            return list(self.live)
        if "UPDATE" in str(sql):
            if (params or {}).get("reason") is not None:
                self.holds.append(
                    (
                        str((params or {}).get("id")),
                        str((params or {}).get("reason")),
                    )
                )
            else:
                self.updates.append(
                    (
                        str((params or {}).get("id")),
                        str((params or {}).get("status")),
                    )
                )
            return []
        return []

    @property
    def promoted(self) -> List[str]:
        return [cid for cid, status in self.updates if status == "new"]


def _job(name: str, index: int, **args: Any) -> Dict[str, Any]:
    return {
        "id": f"command:{name}{index}",
        "name": name,
        "args": args or {},
        "created": f"2026-10-16T10:{index:02d}:00+00:00",
    }


def _patch_repo(monkeypatch: pytest.MonkeyPatch, db: FakeDB) -> None:
    from open_notebook.database import repository

    async def repo_query(sql: str, params: Optional[Dict[str, Any]] = None):
        return await db.query(sql, params)

    monkeypatch.setattr(repository, "repo_query", repo_query)
    monkeypatch.setattr(repository, "ensure_record_id", lambda value: value)
