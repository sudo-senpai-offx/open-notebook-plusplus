"""The admission gate, tested at the two places the review found it wrong.

A design review identified two defects here and both are invisible to ordinary
testing, which is why they survived a full battery:

1. **Per-job cost instead of per-model.** Three transformations on one model
   were charged as three resident models - about 18 GB on a 15 GB device. Under
   RAM-only that is not a slowdown, it is an OOM kill of the largest resident
   process, which is the model server.

2. **No aggregate budget across a pass.** With 20 GB free and a 6 GB model,
   each promotion passed individually and all three were admitted. The rule
   only failed once the device ran out, which is the direction where a wrong
   answer is unrecoverable.

Both are properties of the decision, not of the loop around it, which is why
the rule is a pure function and these tests call it directly.
"""

from typing import List
from unittest.mock import patch

import pytest

from open_notebook.scheduler.footprint import (
    EXPLICIT_FOOTPRINTS,
    JobFootprint,
    job_footprint,
    merge_footprints,
)
from open_notebook.scheduler.gate import (
    GateDecision,
    default_reserve,
    evaluate,
    incremental_cost,
)
from open_notebook.scheduler.memory import MemoryReading

MIB = 1024 * 1024
GIB = 1024 * MIB

#: qwen3:4b Q4_K_M, the model that was actually in use on the PC.
GENERATION_BYTES = 5_093_028_002
GENERATION_COST = int(GENERATION_BYTES * 1.10)


def gen_job() -> JobFootprint:
    return job_footprint("run_transformation", generation_bytes=GENERATION_BYTES)


def cheap_job() -> JobFootprint:
    return job_footprint("process_source")


def memory(
    *, total: int = 15 * GIB, available: int = 8 * GIB, free: int | None = None
) -> MemoryReading:
    return MemoryReading(
        total=total,
        available=available,
        free=available if free is None else free,
        readable=True,
    )


class TestSharedModelCost:
    """The per-model correction."""

    def test_second_job_on_the_same_model_is_free(self):
        committed = merge_footprints([gen_job()])
        assert incremental_cost(committed, gen_job()) == 0

    def test_three_transformations_cost_one_model(self):
        """The reviewed design charged ~18 GB here. One resident model is
        ~5.2 GB, and the device has 15.
        """
        committed = merge_footprints([gen_job()])
        projected = committed.total
        for _ in range(2):
            projected += incremental_cost(committed, gen_job())
        assert projected == GENERATION_COST
        assert projected < 15 * GIB

    def test_a_different_model_does_cost_more(self):
        committed = merge_footprints([gen_job()])
        assert incremental_cost(committed, cheap_job()) > 0
        assert incremental_cost(committed, job_footprint("embed_source")) > 0

    def test_repeated_plumbing_jobs_share_the_no_model_slot(self):
        committed = merge_footprints([cheap_job(), cheap_job()])
        assert incremental_cost(committed, cheap_job()) == 0


class TestWindow:
    def test_full_window_holds(self):
        decision = evaluate(
            window=2,
            in_flight=2,
            committed=merge_footprints([]),
            candidate=cheap_job(),
            memory=memory(),
        )
        assert decision.admit is False
        assert decision.reason == "window-full"

    def test_window_reports_before_memory(self):
        """A full window is the commoner reason and the actionable one.
        Reporting "memory" here would send an operator to look at RAM when
        the fix is fewer concurrent jobs.
        """
        decision = evaluate(
            window=1,
            in_flight=1,
            committed=merge_footprints([gen_job()]),
            candidate=gen_job(),
            # deliberately starved, so memory would also refuse
            memory=memory(available=10 * MIB),
        )
        assert decision.reason == "window-full"

    def test_below_window_proceeds_to_memory(self):
        decision = evaluate(
            window=3,
            in_flight=0,
            committed=merge_footprints([]),
            candidate=cheap_job(),
            memory=memory(),
        )
        assert decision.admit is True

    def test_zero_window_admits_nothing(self):
        """Strict mode with a window of 0 is a legitimate state during a
        manual drain, and must not fall through to the memory checks.
        """
        decision = evaluate(
            window=0,
            in_flight=0,
            committed=merge_footprints([]),
            candidate=cheap_job(),
            memory=memory(),
        )
        assert decision.admit is False
        assert decision.reason == "window-full"


class TestMemoryGate:
    def test_admits_when_there_is_room(self):
        decision = evaluate(
            window=3,
            in_flight=0,
            committed=merge_footprints([]),
            candidate=gen_job(),
            memory=memory(total=15 * GIB, available=10 * GIB),
        )
        assert decision.admit is True
        assert decision.incremental == GENERATION_COST

    def test_holds_when_the_reserve_would_be_breached(self):
        decision = evaluate(
            window=3,
            in_flight=0,
            committed=merge_footprints([]),
            candidate=gen_job(),
            # 6 GB free, 5.2 GB job, ~2.3 GB reserve -> cannot fit
            memory=memory(total=15 * GIB, available=6 * GIB),
        )
        assert decision.admit is False
        assert decision.reason == "memory"

    def test_admits_when_it_exactly_clears_the_reserve(self):
        reserve = default_reserve(15 * GIB)
        decision = evaluate(
            window=3,
            in_flight=0,
            committed=merge_footprints([]),
            candidate=gen_job(),
            memory=memory(
                total=15 * GIB,
                available=GENERATION_COST + reserve,
            ),
        )
        assert decision.admit is True

    def test_holds_one_byte_short(self):
        """Off-by-one direction that matters: one byte less and the device
        is one byte past its floor.
        """
        reserve = default_reserve(15 * GIB)
        decision = evaluate(
            window=3,
            in_flight=0,
            committed=merge_footprints([]),
            candidate=gen_job(),
            memory=memory(total=15 * GIB, available=GENERATION_COST + reserve - 1),
        )
        assert decision.admit is False

    def test_unknown_footprint_is_held_never_assumed_small(self):
        """A generation job whose model size could not be determined.
        Admitting it on the assumption that it is small is an OOM.
        """
        decision = evaluate(
            window=3,
            in_flight=0,
            committed=merge_footprints([]),
            candidate=job_footprint("run_transformation"),
            memory=memory(available=14 * GIB),
        )
        assert decision.admit is False
        assert decision.reason == "unknown-footprint"


class TestCeilingAgainstCacheInflation:
    """The check that MemAvailable alone cannot make.

    On these devices `MemAvailable` counts reclaimable page cache, where the
    model blobs live. A device can therefore report comfortable headroom and
    still be unable to fault the pages in.
    """

    def test_cache_inflated_availability_does_not_bypass_the_ceiling(self):
        # 7.5 GB "available" on an 8 GB device, all of it cache. The reserve
        # is zeroed so it cannot be what refuses: only the ceiling can.
        decision = evaluate(
            window=3,
            in_flight=0,
            committed=merge_footprints([]),
            candidate=JobFootprint(models={"big": 7 * GIB}),
            memory=MemoryReading(
                total=8 * GIB,
                available=7_500 * MIB,
                free=300 * MIB,
                readable=True,
            ),
            reserve=0,
        )
        assert decision.admit is False
        assert decision.reason == "memory"
        # 8 GB * 0.85 = 6.8 GB, and the job wanted 7 GB.
        assert 7 * GIB > 8 * GIB * 0.85

    def test_the_same_job_is_admitted_when_it_fits_the_ceiling(self):
        decision = evaluate(
            window=3,
            in_flight=0,
            committed=merge_footprints([]),
            candidate=JobFootprint(models={"big": 6 * GIB}),
            memory=MemoryReading(
                total=8 * GIB,
                available=7_500 * MIB,
                free=300 * MIB,
                readable=True,
            ),
            reserve=0,
        )
        assert decision.admit is True

    def test_ceiling_refuses_even_with_comfortable_availability(self):
        # Two distinct models already resident, plus a third that would cross
        # the ceiling, on a device whose "available" figure is mostly cache.
        committed = merge_footprints(
            [gen_job(), job_footprint("embed_source"), cheap_job()]
        )
        decision = evaluate(
            window=9,
            in_flight=0,
            committed=committed,
            candidate=JobFootprint(models={"second_model": 4 * GIB}),
            memory=MemoryReading(
                total=8 * GIB,
                available=7 * GIB,
                free=300 * MIB,
                readable=True,
            ),
            reserve=0,
        )
        assert decision.admit is False
        assert decision.reason == "memory"

    def test_ceiling_fraction_is_configurable(self):
        committed = merge_footprints([JobFootprint(models={"a": 7 * GIB})])
        candidate = JobFootprint(models={"b": 2 * GIB})
        generous = evaluate(
            window=5,
            in_flight=0,
            committed=committed,
            candidate=candidate,
            memory=memory(total=16 * GIB, available=8 * GIB),
            reserve=0,
            ceiling_fraction=0.95,
        )
        tight = evaluate(
            window=5,
            in_flight=0,
            committed=committed,
            candidate=candidate,
            memory=memory(total=16 * GIB, available=8 * GIB),
            reserve=0,
            ceiling_fraction=0.5,
        )
        assert generous.admit is True
        assert tight.admit is False


class TestAggregateBudgetAcrossAPass:
    """The second reviewed bug: three passes of individually-valid
    promotions that together exceed the device.
    """

    def test_three_admissions_never_exceed_the_device(self):
        """Three transformations on one model are all safe, and the proof is
        that the projection never grows.

        The reviewed design would have charged each of the three its own model
        - roughly 18 GB against a device with 8 - and refused the third for
        the wrong reason, or admitted it and taken the OOM.
        """
        total = 8 * GIB
        available = total - 100 * MIB
        committed = merge_footprints([])
        in_flight = 0
        projections: List[int] = []

        for _ in range(3):
            decision = evaluate(
                window=3,
                in_flight=in_flight,
                committed=committed,
                candidate=gen_job(),
                memory=MemoryReading(
                    total=total,
                    available=available,
                    free=available,
                    readable=True,
                ),
            )
            if not decision.admit:
                break
            projections.append(decision.projected)
            in_flight += 1
            committed = merge_footprints([gen_job()])

        assert len(projections) == 3, "the shared model should never block the window"
        assert len(set(projections)) == 1, "projection must not grow per admission"
        assert projections[0] == GENERATION_COST
        assert projections[0] <= total * 0.85

    def test_incremental_cost_is_zero_after_the_first(self):
        committed = merge_footprints([])
        costs: List[int] = []
        for index in range(3):
            costs.append(incremental_cost(committed, gen_job()))
            committed = committed.merge(merge_footprints([gen_job()]))
        assert costs == [GENERATION_COST, 0, 0]

    def test_distinct_models_stop_at_the_ceiling(self):
        """Three *different* large models on a small device. Individually
        each clears the reserve; together they cannot.
        """
        total = 4 * GIB
        committed = merge_footprints([])
        in_flight = 0
        admitted = 0
        for index in range(4):
            decision = evaluate(
                window=4,
                in_flight=in_flight,
                committed=committed,
                candidate=JobFootprint(models={f"model_{index}": 1_400 * MIB}),
                memory=MemoryReading(
                    total=total,
                    available=total - 50 * MIB,
                    free=total - 50 * MIB,
                    readable=True,
                ),
                reserve=0,
            )
            if not decision.admit:
                break
            admitted += 1
            in_flight += 1
            committed = committed.merge(
                merge_footprints([JobFootprint(models={f"model_{index}": 1_400 * MIB})])
            )

        # 4 GB * 0.85 = 3.4 GB, so two models fit and the third is refused.
        assert admitted == 2
        assert committed.total <= total * 0.85


class TestUnreadableMemory:
    """Neither wedge nor recklessness."""

    def test_degrades_to_one_job_not_a_permanent_hold(self):
        unreadable = MemoryReading()
        first = evaluate(
            window=3,
            in_flight=0,
            committed=merge_footprints([]),
            candidate=gen_job(),
            memory=unreadable,
        )
        second = evaluate(
            window=3,
            in_flight=1,
            committed=merge_footprints([gen_job()]),
            candidate=gen_job(),
            memory=unreadable,
        )
        assert first.admit is True
        assert first.reason == "memory-unreadable"
        # The second is stopped by serialisation, not by the window.
        assert second.admit is False

    def test_still_respects_the_window(self):
        """Serialising is the fallback, not ignoring the window. A window of
        1 with one job already in flight is full, and admitting a second
        would defeat the point of degrading.
        """
        decision = evaluate(
            window=1,
            in_flight=1,
            committed=merge_footprints([gen_job()]),
            candidate=cheap_job(),
            memory=MemoryReading(),
        )
        assert decision.admit is False
        assert decision.reason == "window-full"

    def test_window_larger_than_one_still_serialises(self):
        """The degradation caps at one job even when the operator's window
        is wider, because without a memory reading nothing else is known.
        """
        decision = evaluate(
            window=3,
            in_flight=1,
            committed=merge_footprints([gen_job()]),
            candidate=cheap_job(),
            memory=MemoryReading(),
        )
        assert decision.admit is False
        assert decision.reason == "memory-unreadable"


class TestReserve:
    def test_absolute_floor_protects_the_floor_class(self):
        """A percentage of 3 GB is under 500 MB, which is not enough to keep
        the shell, database and UI alive while a model loads.
        """
        assert default_reserve(3 * GIB) == 512 * MIB

    def test_percentage_takes_over_on_a_large_device(self):
        assert default_reserve(32 * GIB) == int(32 * GIB * 0.15)


class TestModelPool:
    """Where the models live, which is not always where this code lives.

    Found by running it: on the PC the worker is in a Docker container and
    Ollama is on the host, and Docker Desktop virtualises `/proc` so the
    container sees the VM's 3.56 GB rather than the host's 15 GB. Comparing a
    5.2 GB host-resident model against a 3.03 GB ceiling holds every
    generation job forever - and the gate reports `memory` throughout, which
    is true of the number it was given.
    """

    def test_defaults_to_this_processes_own_memory(self):
        """The phone's case: the model is local, so /proc is correct."""
        from open_notebook.scheduler.gate import model_pool_bytes

        reading = memory(total=8 * GIB)
        with patch.dict("os.environ", {}, clear=False):
            import os

            os.environ.pop("OPEN_NOTEBOOK_MODEL_POOL_BYTES", None)
            assert model_pool_bytes(reading) == 8 * GIB

    def test_override_is_honoured(self):
        from open_notebook.scheduler.gate import model_pool_bytes

        with patch.dict(
            "os.environ", {"OPEN_NOTEBOOK_MODEL_POOL_BYTES": str(15 * GIB)}
        ):
            assert model_pool_bytes(memory(total=3_560_000_000)) == 15 * GIB

    @pytest.mark.parametrize("bad", ["not-a-number", "0", "-1", ""])
    def test_a_nonsense_override_falls_back_rather_than_blocking(self, bad):
        """A typo in the setting must not hold every job. Falling back to
        the local total is the pre-existing behaviour and is wrong on the PC
        but recoverable; holding everything is neither.
        """
        from open_notebook.scheduler.gate import model_pool_bytes

        with patch.dict("os.environ", {"OPEN_NOTEBOOK_MODEL_POOL_BYTES": bad}):
            assert model_pool_bytes(memory(total=8 * GIB)) == 8 * GIB

    def test_a_generation_job_is_admitted_when_the_pool_is_configured(self):
        """The end-to-end shape of the bug, as a test: a model larger than
        this process's own total must still be admit-able when the models
        demonstrably live elsewhere.
        """
        decision = evaluate(
            window=3,
            in_flight=0,
            committed=merge_footprints([]),
            candidate=gen_job(),
            # A container that believes it has 3.5 GB...
            memory=memory(total=3_560_000_000, available=1_960_000_000),
            reserve=0,
            # ...with the models on a 15 GB host.
            model_pool=15 * GIB,
        )
        assert decision.admit is True

    def test_the_same_job_is_held_when_the_pool_is_the_local_one(self):
        """And the honest refusal when they are the same pool - which is the
        phone, and where the ceiling is the check that matters.
        """
        decision = evaluate(
            window=3,
            in_flight=0,
            committed=merge_footprints([]),
            candidate=gen_job(),
            memory=memory(total=3_560_000_000, available=1_960_000_000),
            reserve=0,
        )
        assert decision.admit is False
        assert decision.reason == "memory"


class TestDecisionShape:
    def test_reason_is_always_one_of_the_contract_values(self):
        allowed = {
            "ok",
            "window-full",
            "memory",
            "unknown-footprint",
            "memory-unreadable",
        }
        cases = [
            evaluate(
                window=1,
                in_flight=1,
                committed=merge_footprints([]),
                candidate=cheap_job(),
                memory=memory(),
            ),
            evaluate(
                window=3,
                in_flight=0,
                committed=merge_footprints([]),
                candidate=gen_job(),
                memory=memory(available=MIB),
            ),
            evaluate(
                window=3,
                in_flight=0,
                committed=merge_footprints([]),
                candidate=job_footprint("run_transformation"),
                memory=memory(),
            ),
            evaluate(
                window=3,
                in_flight=0,
                committed=merge_footprints([]),
                candidate=gen_job(),
                memory=MemoryReading(),
            ),
            evaluate(
                window=3,
                in_flight=0,
                committed=merge_footprints([]),
                candidate=cheap_job(),
                memory=memory(),
            ),
        ]
        for decision in cases:
            assert isinstance(decision, GateDecision)
            assert decision.reason in allowed

    def test_held_decisions_report_zero_projection(self):
        """`projected` is what *would* be resident. On a refusal there is no
        such thing, and reporting a number there invites a caller to log
        memory that was never committed.
        """
        held = evaluate(
            window=0,
            in_flight=0,
            committed=merge_footprints([]),
            candidate=gen_job(),
            memory=memory(),
        )
        assert held.admit is False
        assert held.projected == 0

    def test_plumbing_footprint_is_small(self):
        assert cheap_job().total == EXPLICIT_FOOTPRINTS["__no_model__"]


@pytest.mark.parametrize("window", [0, 1, 2, 3, 8])
def test_no_configuration_can_admit_without_headroom(window: int):
    """Property: for any window, a device with no available memory admits
    nothing that has a real footprint.
    """
    decision = evaluate(
        window=window,
        in_flight=0,
        committed=merge_footprints([]),
        candidate=gen_job(),
        memory=memory(total=15 * GIB, available=0),
    )
    assert decision.admit is False
    assert decision.reason in {"window-full", "memory"}
