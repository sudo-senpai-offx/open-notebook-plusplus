"""The memory reader and the footprint model, which the gate depends on.

Both were assumptions in the reviewed design and both turned out to be missing
rather than merely wrong, so they are pinned here directly. The bias to test
for is specific: a reader that reports an unreadable file as "zero free" holds
every job forever, and a reader that reports it as "unlimited" promotes
everything. Both look like a working gate.
"""

from pathlib import Path

import pytest

from open_notebook.scheduler.footprint import (
    EXPLICIT_FOOTPRINTS,
    WEIGHT_OVERHEAD,
    FootprintEstimate,
    JobFootprint,
    job_footprint,
    merge_footprints,
    model_footprint,
)
from open_notebook.scheduler.memory import MemoryReading, read_memory

MIB = 1024 * 1024
GIB = 1024 * MIB


def _meminfo(**kwargs: int) -> str:
    fields = {
        "MemTotal": kwargs.get("total_kb", 0),
        "MemFree": kwargs.get("free_kb", 0),
        "MemAvailable": kwargs.get("available_kb", 0),
        "SwapTotal": kwargs.get("swap_total_kb", 0),
        "SwapFree": kwargs.get("swap_free_kb", 0),
    }
    lines = [f"{name}: {value:>15} kB" for name, value in fields.items()]
    # Real files carry many more fields; the parser must ignore them.
    lines.append("HugePages_Total:       0 kB")
    lines.append("SomeUnknownFutureField: 12345 kB")
    return "\n".join(lines) + "\n"


class TestMemoryReading:
    def test_parses_the_fields_the_gate_uses(self, tmp_path: Path):
        path = tmp_path / "meminfo"
        path.write_text(
            _meminfo(
                total_kb=15320 * 1024,
                free_kb=158 * 1024,
                available_kb=525 * 1024,
                swap_total_kb=32170 * 1024,
                swap_free_kb=14646 * 1024,
            )
        )

        reading = read_memory(path)

        assert reading.readable is True
        assert reading.total == 15320 * MIB
        assert reading.available == 525 * MIB
        assert reading.free == 158 * MIB
        assert reading.swapped == (32170 - 14646) * MIB

    def test_missing_file_is_unreadable_not_zero(self, tmp_path: Path):
        """The dangerous reading. Zero available holds every job forever;
        the caller must be able to tell "unknown" from "none"."""
        reading = read_memory(tmp_path / "absent")

        assert reading.readable is False
        assert reading.total == 0
        assert reading.available_or_none is None

    def test_file_without_memtotal_is_unreadable(self, tmp_path: Path):
        """A truncated or proc file from a future kernel. Silently
        reporting 0 bytes total would make every ratio below meaningless.
        """
        path = tmp_path / "meminfo"
        path.write_text("SomeUnknownFutureField: 12345 kB\n")

        assert read_memory(path).readable is False

    def test_empty_file_is_unreadable(self, tmp_path: Path):
        path = tmp_path / "meminfo"
        path.write_text("")
        assert read_memory(path).readable is False

    def test_ignores_unknown_fields_and_bad_lines(self, tmp_path: Path):
        path = tmp_path / "meminfo"
        path.write_text(
            "garbage line with no colon\n"
            "MemTotal: not-a-number kB\n"
            + _meminfo(total_kb=1024 * 1024, available_kb=512 * 1024)
        )
        # MemTotal appears twice; the usable numeric one wins because the
        # garbage line does not match the pattern at all.
        reading = read_memory(path)
        assert reading.readable is True
        assert reading.total == 1024 * MIB

    def test_real_host_file_parses(self):
        """Not a mock. If this fails, the gate has no input at all."""
        reading = read_memory(Path("/proc/meminfo"))
        if not reading.readable:
            pytest.skip("no /proc/meminfo in this environment")
        assert reading.total > 0
        assert reading.available >= 0
        assert reading.available <= reading.total


class TestCacheInflation:
    def test_flags_available_that_is_mostly_cache(self):
        """`MemAvailable` counts reclaimable page cache. On these devices
        that cache holds model blobs and RocksDB buffers, so a large
        available-minus-free gap means the gate is being handed a number it
        should not trust on its own.
        """
        reading = MemoryReading(
            total=8 * GIB,
            available=3 * GIB,
            free=int(200 * MIB),
            readable=True,
        )
        assert reading.cache_inflated is True

    def test_genuinely_free_memory_is_not_flagged(self):
        reading = MemoryReading(
            total=8 * GIB,
            available=3 * GIB,
            free=int(2.8 * GIB),
            readable=True,
        )
        assert reading.cache_inflated is False

    def test_unreadable_is_never_cache_inflated(self):
        assert MemoryReading().cache_inflated is False


class TestModelFootprint:
    def test_derives_from_disk_size(self):
        # qwen3:4b Q4_K_M is 5.09 GB on disk. The result is asserted in GiB
        # because 1 GiB != 1 GB and the earlier version of this bound
        # conflated them, which is how a wrong test can hide a right answer.
        cost = model_footprint("generation", 5_093_028_002)
        assert cost == int(5_093_028_002 * WEIGHT_OVERHEAD)
        assert 5.0 * GIB < cost < 5.5 * GIB

    def test_explicit_entry_wins_over_disk_size(self):
        """A stated cost beats a derived one. Deriving from a blob file for a
        model that is small on purpose would over-reserve it."""
        assert model_footprint("bge-m3", 999 * GIB) == EXPLICIT_FOOTPRINTS["bge-m3"]

    def test_unknown_model_with_no_size_is_zero(self):
        assert model_footprint("mystery", None) == 0

    def test_zero_or_negative_disk_size_is_not_a_size(self):
        assert model_footprint("mystery", 0) == 0
        assert model_footprint("mystery", -1) == 0


class TestJobFootprint:
    def test_plumbing_job_needs_no_model(self):
        result = job_footprint("process_source")
        assert result.uncertain is False
        assert result.total == EXPLICIT_FOOTPRINTS["__no_model__"]

    def test_generation_job_carries_the_generation_model(self):
        result = job_footprint("run_transformation", generation_bytes=5_093_028_002)
        assert result.uncertain is False
        assert "generation" in result.models

    def test_embedding_job_carries_the_embedding_model(self):
        result = job_footprint("embed_source", embedding_bytes=None)
        # bge-m3 is an explicit entry, so it is known without a disk size.
        assert result.uncertain is False
        assert "embedding" in result.models

    def test_unsized_generation_is_uncertain_not_free(self):
        """The dangerous direction. A generation job whose model size cannot
        be determined must be held, never admitted on an assumption that it
        is small.
        """
        result = job_footprint("run_transformation", generation_bytes=None)
        assert result.uncertain is True

    def test_embedding_is_known_without_a_disk_size(self):
        """`job_footprint` knows the *category*, not the model, so it looks
        the cost up under "embedding". An earlier version of the table had
        only model names ("bge-m3"), every embedding job resolved to zero
        cost, and every one of them reported itself uncertain - a gate that
        would have held all embedding work indefinitely.
        """
        result = job_footprint("embed_note")
        assert result.uncertain is False
        assert result.models == {"embedding": EXPLICIT_FOOTPRINTS["embedding"]}

    def test_unclassified_command_defaults_to_cheap(self):
        """Most commands are plumbing. Defaulting to *expensive* would make
        the gate useless; the cost is that an unclassified expensive command
        is under-reserved, so the table has to stay complete.
        """
        result = job_footprint("some_new_command_nobody_classified")
        assert result.uncertain is False
        assert result.total == EXPLICIT_FOOTPRINTS["__no_model__"]


class TestFootprintAggregation:
    def test_three_jobs_on_one_model_cost_one_model(self):
        """The correction the review forced.

        The reviewed design charged each job its own model footprint, which
        meant three transformations on one model looked like three resident
        models - roughly 18 GB on a 15 GB device - and would have held work
        that was entirely safe. The unit of cost is the model.
        """
        one = job_footprint("run_transformation", generation_bytes=5_093_028_002)
        merged = merge_footprints([one, one, one])

        assert merged.total == one.total
        assert list(merged.models) == ["generation"]

    def test_different_models_add_up(self):
        generation = job_footprint("run_transformation", generation_bytes=5_093_028_002)
        embedding = job_footprint("embed_source")
        merged = merge_footprints([generation, embedding])

        assert merged.total == generation.total + embedding.total
        assert set(merged.models) == {"generation", "embedding"}

    def test_plumbing_jobs_share_the_no_model_slot(self):
        parts = [
            job_footprint("process_source"),
            job_footprint("crawl_site"),
            job_footprint("create_insight"),
        ]
        assert merge_footprints(parts).total == EXPLICIT_FOOTPRINTS["__no_model__"]

    def test_uncertainty_surfaces_as_a_named_sentinel(self):
        """A caller that forgets to check must still see something it cannot
        mistake for a real model, rather than a zero cost that reads as
        free.
        """
        merged = merge_footprints(
            [job_footprint("run_transformation"), job_footprint("embed_source")]
        )
        assert "__unknown__" in merged.models
        assert merged.models["__unknown__"] == 0

    def test_merging_an_estimate_with_itself_is_idempotent(self):
        """The dispatcher re-derives the in-flight set every pass, so this
        runs repeatedly. A merge that grew on each pass would inflate the
        estimate until the gate held everything.
        """
        parts = [
            job_footprint("run_transformation", generation_bytes=5_093_028_002),
            job_footprint("embed_source"),
        ]
        first = merge_footprints(parts)
        second = first.merge(first)
        assert second.total == first.total

    def test_merge_takes_the_larger_of_a_shared_model(self):
        """max(), not sum(): two sets naming the same model are paying for
        one resident copy, and one of them may have measured it better.
        """
        small = FootprintEstimate(models={"generation": 100})
        large = FootprintEstimate(models={"generation": 900})
        assert small.merge(large).total == 900
        assert large.merge(small).total == 900

    def test_empty_set_costs_nothing(self):
        assert merge_footprints([]).total == 0

    def test_key_set_identifies_shared_models(self):
        merged = merge_footprints(
            [
                job_footprint("run_transformation", generation_bytes=5_093_028_002),
                job_footprint("run_transformation", generation_bytes=5_093_028_002),
            ]
        )
        assert merged.key_set() == frozenset({"generation"})


def test_footprint_model_is_independent_of_the_database() -> None:
    """A scheduling decision must not make a network call.

    `job_footprint` takes model sizes as arguments precisely so this holds.
    The check looks for imports and calls, not bare words: an earlier version
    matched the substring "requests" and failed on this module's own
    docstring, which is the kind of test that teaches people to ignore it.
    """
    import ast
    import inspect

    import open_notebook.scheduler.footprint as module

    tree = ast.parse(inspect.getsource(module))
    imported: set[str] = set()
    called: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            called.add(node.func.id)
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            called.add(node.func.attr)

    assert not (imported & {"httpx", "requests", "aiohttp", "surreal_commands"})
    # Attribute calls are excluded from the name check: `dict.get`, which this
    # module legitimately uses, matched an earlier version of this test and
    # made it fail on its own correct code.
    assert not (called & {"repo_query", "urlopen", "urlretrieve"})


def test_uncertainty_has_a_typed_representation() -> None:
    """`JobFootprint.uncertain` is the field the gate branches on, so it must
    exist on the type rather than being inferred from an empty dict.
    """
    result = job_footprint("run_transformation")
    assert isinstance(result, JobFootprint)
    assert result.uncertain is True
    assert result.models == {}
