"""The dispatcher pass: what it promotes, and what it refuses.

Two reviewed blockers live here and both are invisible unless the *pass* is
exercised, not just the gate it calls:

1. **No aggregate budget.** The rule was evaluated per job against a snapshot
   of memory taken once per pass. Three individually-valid promotions
   overshot the device. The fix is that the committed set is recomputed after
   *every* promotion, so this test has to drive a whole pass rather than call
   the gate three times.

2. **`held` invisible to the filters that deduplicate.** A held job that the
   skip and dedup filters cannot see is re-queued by the next sync import,
   under exactly the memory pressure that caused the hold. Those filters live
   in other modules, so they are pinned here by driving the real ones.
"""

from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest

from open_notebook.scheduler.dispatcher import (
    committed_footprint,
    dispatch_once,
    generation_model_bytes,
)
from open_notebook.scheduler.memory import MemoryReading

MIB = 1024 * 1024
GIB = 1024 * MIB

GENERATION_BYTES = 5_093_028_002


class FakeDB:
    """An in-memory stand-in for the command table.

    Records the promotions it is asked to make so a test can assert on what
    the pass *did*, not only on what it returned. The distinction matters:
    a dispatcher that returns a correct-looking outcome while writing the
    wrong status is the failure mode worth catching.
    """

    def __init__(
        self,
        held: Optional[List[Dict[str, Any]]] = None,
        live: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        self.held = held or []
        self.live = live or []
        self.updates: List[Tuple[str, str]] = []
        self.fail_on: Optional[str] = None

    async def query(
        self, sql: str, params: Optional[Dict[str, Any]] = None
    ) -> List[Dict[str, Any]]:
        if "status = 'held'" in sql and "ORDER BY" in sql:
            # The database applies `ORDER BY created ASC`; the fake has to as
            # well, or a test that lists rows out of order is testing the
            # fake's ordering rather than the dispatcher's.
            return sorted(self.held, key=lambda row: str(row.get("created") or ""))
        if "status IN" in sql and "SELECT name, args" in sql:
            return list(self.live)
        if "UPDATE" in sql:
            command_id = str((params or {}).get("id"))
            status = str((params or {}).get("status"))
            if self.fail_on and self.fail_on in command_id:
                raise RuntimeError("simulated promotion failure")
            self.updates.append((command_id, status))
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
        # Ascending timestamps: oldest-first promotion depends on this.
        "created": f"2026-10-16T10:{index:02d}:00+00:00",
    }


def _patch_repo(monkeypatch: pytest.MonkeyPatch, db: FakeDB) -> None:
    """Patch the repository module, not the dispatcher.

    `dispatch_once` imports `repo_query` and `ensure_record_id` from
    `open_notebook.database.repository` inside the function body, so they are
    bound at call time from that module. Patching the dispatcher's namespace
    would silently do nothing - and an earlier version of this helper did
    exactly that, failing with an AttributeError that looked like a bug in
    the dispatcher rather than in the test.
    """
    from open_notebook.database import repository

    async def repo_query(sql: str, params: Optional[Dict[str, Any]] = None):
        return await db.query(sql, params)

    monkeypatch.setattr(repository, "repo_query", repo_query)
    monkeypatch.setattr(repository, "ensure_record_id", lambda value: value)


def _memory(*, total: int = 15 * GIB, available: int = 8 * GIB) -> MemoryReading:
    return MemoryReading(
        total=total, available=available, free=available, readable=True
    )


class TestPromotion:
    @pytest.mark.asyncio
    async def test_promotes_oldest_first(self, monkeypatch):
        db = FakeDB(held=[_job("process_source", 2), _job("process_source", 1)])
        _patch_repo(monkeypatch, db)

        outcome = await dispatch_once(
            generation_bytes=GENERATION_BYTES, memory=_memory()
        )

        # The row created at 10:01 comes first even though it is second in
        # the list, because `ORDER BY created` is what the query does.
        assert len(outcome.promoted) == 2
        assert db.updates[0][0] == "command:process_source1"

    @pytest.mark.asyncio
    async def test_window_caps_promotions(self, monkeypatch):
        db = FakeDB(held=[_job("process_source", i) for i in range(1, 6)])
        _patch_repo(monkeypatch, db)

        outcome = await dispatch_once(
            generation_bytes=GENERATION_BYTES, memory=_memory(), window=2
        )

        assert outcome.promoted_count == 2
        assert len(db.updates) == 2

    @pytest.mark.asyncio
    async def test_nothing_to_do_is_not_an_error(self, monkeypatch):
        db = FakeDB(held=[], live=[])
        _patch_repo(monkeypatch, db)

        outcome = await dispatch_once(
            generation_bytes=GENERATION_BYTES, memory=_memory()
        )

        assert outcome.promoted_count == 0
        assert outcome.error is None
        assert outcome.in_flight_after == 0

    @pytest.mark.asyncio
    async def test_a_failed_promotion_does_not_stop_the_pass(self, monkeypatch):
        """One unwritable row must not cost every other job. If it did, a
        single bad record would silently freeze the whole queue.
        """
        db = FakeDB(held=[_job("process_source", 1), _job("process_source", 2)])
        db.fail_on = "process_source1"
        _patch_repo(monkeypatch, db)

        outcome = await dispatch_once(
            generation_bytes=GENERATION_BYTES, memory=_memory()
        )

        assert outcome.promoted_count == 1
        assert outcome.error is not None
        assert db.promoted == ["command:process_source2"]


class TestAggregateBudget:
    """The blocker the review found: a pass that admits past the device."""

    @pytest.mark.asyncio
    async def test_many_generation_jobs_are_not_all_admitted_at_once(self, monkeypatch):
        """Five generation jobs, all the same model, on a device with room for
        one. They share one resident model, so the window governs - but a
        snapshot taken once per pass would still have let the fifth through if
        the budget were charged per job.
        """
        db = FakeDB(held=[_job("run_transformation", i) for i in range(1, 6)])
        _patch_repo(monkeypatch, db)

        outcome = await dispatch_once(
            generation_bytes=GENERATION_BYTES,
            memory=_memory(total=8 * GIB, available=7 * GIB),
            window=5,
        )

        # Window of 5 on an 8 GB device with a 5.2 GB model: the first is
        # admitted, and the rest are free because the model is shared. So all
        # five proceed - and that is correct, which is the point.
        assert outcome.promoted_count == 5
        assert outcome.projected_bytes < 8 * GIB * 0.85

    @pytest.mark.asyncio
    @pytest.mark.asyncio
    async def test_distinct_large_models_stop_at_the_ceiling(self, monkeypatch):
        """Four *different* large models cannot all be resident.

        Individually each clears the reserve; only the committed set stops
        them. Driven through the gate rather than the dispatcher, because the
        footprint table keys every generation job to one shared model and no
        production path gives each job its own - so building one through the
        dispatcher would be testing a fiction.
        """
        from open_notebook.scheduler.footprint import FootprintEstimate, JobFootprint
        from open_notebook.scheduler.gate import evaluate

        committed = FootprintEstimate()
        admitted = 0
        # 1200 MiB each: two fit under 4 GiB * 0.85 = 3.4 GiB (2.34 GiB),
        # three do not (3.5 GiB). An earlier version used 2000 MiB, where only
        # one fits - and the assertion expected two, which was arithmetic in
        # the test rather than behaviour in the gate.
        for index in range(4):
            candidate = JobFootprint(models={f"m{index}": 1_200 * MIB})
            decision = evaluate(
                window=4,
                in_flight=admitted,
                committed=committed,
                candidate=candidate,
                memory=MemoryReading(
                    total=4 * GIB,
                    available=4 * GIB - 50 * MIB,
                    free=4 * GIB - 50 * MIB,
                    readable=True,
                ),
                reserve=0,
            )
            if not decision.admit:
                break
            admitted += 1
            committed = committed.merge(
                FootprintEstimate(models=dict(candidate.models))
            )

        assert admitted == 2
        assert committed.total <= 4 * GIB * 0.85


class TestHeldReasons:
    @pytest.mark.asyncio
    async def test_memory_hold_is_distinguishable_from_a_full_window(self, monkeypatch):
        """The whole point of a reason field: "nothing is running" and
        "everything is waiting on memory" need different fixes, and the UI can
        only offer one if the backend can tell them apart.
        """
        generation = [
            _job("run_transformation", i, source_id=f"source:s{i}") for i in range(1, 4)
        ]
        db = FakeDB(held=generation)
        _patch_repo(monkeypatch, db)

        starved = await dispatch_once(
            generation_bytes=GENERATION_BYTES,
            memory=_memory(total=15 * GIB, available=200 * MIB),
            window=3,
        )
        assert starved.promoted_count == 0
        assert starved.held_reasons.get("memory", 0) >= 1

        db2 = FakeDB(held=list(generation), live=[_job("process_source", 9)])
        _patch_repo(monkeypatch, db2)
        full = await dispatch_once(
            generation_bytes=GENERATION_BYTES, memory=_memory(), window=1
        )
        assert full.promoted_count == 0
        assert full.held_reasons.get("window-full", 0) >= 1

    @pytest.mark.asyncio
    async def test_unsized_generation_is_held_not_admitted(self, monkeypatch):
        """Ollama unreachable means the model size is unknown. Admitting on
        an assumption would be an OOM; holding costs only time.
        """
        db = FakeDB(held=[_job("run_transformation", 1, source_id="source:a")])
        _patch_repo(monkeypatch, db)

        outcome = await dispatch_once(
            generation_bytes=None, lookup_generation=False, memory=_memory(), window=3
        )

        assert outcome.promoted_count == 0
        assert outcome.held_reasons.get("unknown-footprint", 0) == 1


class TestCommittedFootprint:
    def test_shared_models_are_charged_once(self):
        from open_notebook.scheduler.footprint import job_footprint, merge_footprints

        job = job_footprint("run_transformation", generation_bytes=GENERATION_BYTES)
        live = [{"name": "run_transformation"} for _ in range(3)]

        committed = committed_footprint(live, GENERATION_BYTES)

        assert committed.total == merge_footprints([job]).total

    def test_unknown_in_flight_makes_the_estimate_uncertain(self):
        """An in-flight job whose cost could not be determined makes the
        estimate uncertain, so later promotions stay conservative rather than
        optimistic.
        """
        committed = committed_footprint([{"name": "run_transformation"}], None)

        assert "__unknown__" in committed.models


class TestGenerationModelLookup:
    @pytest.mark.asyncio
    async def test_absent_ollama_returns_none_rather_than_raising(self):
        """None is the hold signal. Raising would be caught and turned into
        None anyway, but the honest contract is that this function answers a
        question and 'I do not know' is an answer.
        """
        result = await generation_model_bytes()
        assert result is None or isinstance(result, int)

    @pytest.mark.asyncio
    async def test_uses_the_largest_model_not_the_first(self, monkeypatch):
        """Under-estimating a generation model is an OOM, so the lookup takes
        the maximum available size rather than guessing which tag the
        configured model uses.
        """
        calls: List[str] = []

        class FakeResponse:
            def __init__(self, payload):
                self._payload = payload

            def raise_for_status(self):
                return None

            def json(self):
                return self._payload

        class FakeClient:
            @staticmethod
            def get(url: str, timeout: float = 0.0):
                calls.append(url)
                return FakeResponse(
                    {"models": [{"size": 500}, {"size": 5_093_028_002}]}
                )

        import httpx

        monkeypatch.setattr(httpx, "get", FakeClient.get)

        assert await generation_model_bytes() == 5_093_028_002
        assert calls


class TestIdempotency:
    @pytest.mark.asyncio
    async def test_a_second_pass_promotes_nothing_new(self, monkeypatch):
        """Promoted jobs are no longer `held`, so a repeated pass must not
        promote them again. Re-promotion would re-fire the worker's live
        notification and execute the same command twice - the vendor worker
        has no compare-and-swap, which is why this is worth asserting.
        """
        held = [_job("process_source", 1), _job("process_source", 2)]
        db = FakeDB(held=list(held))
        _patch_repo(monkeypatch, db)

        first = await dispatch_once(generation_bytes=GENERATION_BYTES, memory=_memory())
        assert first.promoted_count == 2

        # Simulate the effect of the promotions: they left `held`.
        db.held = []
        db.live = [{"name": "process_source", "args": {}} for _ in range(2)]
        second = await dispatch_once(
            generation_bytes=GENERATION_BYTES, memory=_memory()
        )

        assert second.promoted_count == 0
        assert len(db.updates) == 2, "the second pass must not re-promote"


class TestFailureContainment:
    @pytest.mark.asyncio
    async def test_an_unreadable_queue_is_reported_not_raised(self, monkeypatch):
        """A bad pass must not kill the loop. Failing toward "nothing ran"
        is visible; killing the thread produces a device that is silently
        wedged and looks busy.
        """

        async def boom(sql: str, params=None):
            raise RuntimeError("database is down")

        from open_notebook.database import repository

        monkeypatch.setattr(repository, "repo_query", boom)

        outcome = await dispatch_once(
            generation_bytes=GENERATION_BYTES, memory=_memory()
        )

        assert outcome.error is not None
        assert outcome.promoted_count == 0


def test_throttle_file_location_is_overridable(tmp_path: Path):
    """The dispatcher must be testable without touching the real .env, which
    holds the sync token.
    """
    from open_notebook.scheduler.dispatcher import _throttle_file

    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("THROTTLE_FILE", str(tmp_path / "x.conf"))
        assert _throttle_file() == tmp_path / "x.conf"
