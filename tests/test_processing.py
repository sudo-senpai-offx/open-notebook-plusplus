"""Processing control: throttle visibility, scheduled ensure, explicit retry.

Bulk insight generation must be idempotent (re-running after an
interruption fills gaps, never duplicates), ordered smallest-first (so
results land early and problem files surface first), and honest about
what it skips. Retries must touch only failed/canceled extractions,
never live jobs or finished work.
"""

from typing import List
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from open_notebook.scheduler.memory import MemoryReading


@pytest.fixture
def client():
    from api.main import app

    return TestClient(app)


def _source(sid, size=1000, text="body text"):
    return {
        "id": sid,
        "title": sid,
        "asset": {"sha256": "a" * 64, "filename": "f.pdf", "size": size},
        "full_text": text,
    }


def _repo_for(sources, transformations=(), insights=(), commands=()):
    async def fake(query, params=None):
        q = str(query)
        if "FROM transformation" in q and "VALUE id" not in q:
            return list(transformations)
        if "FROM source_insight" in q:
            return list(insights)
        if "FROM command" in q:
            return list(commands)
        if "FROM source WHERE id" in q:
            return [dict(sources[0])] if sources else []
        if "FROM source" in q:
            return [dict(s) for s in sources]
        return []

    return fake


class TestEnsureInsights:
    def test_skips_existing_pairs_and_textless_sources(self, client):
        sources = [
            _source("source:done", size=5000),
            _source("source:new", size=100),
            _source("source:empty", size=50, text="   "),
        ]
        transformations = [
            {"id": "transformation:t", "title": "Dense Summary"},
        ]
        insights = [
            {"source": "source:done", "insight_type": "Dense Summary"},
        ]
        submitted: List[dict] = []

        def fake_submit(app, command, args):
            submitted.append(args)
            return "command:x"

        async def fake_repo(query, params=None):
            q = str(query)
            if "FROM transformation" in q and "VALUE" not in q:
                return transformations
            if "FROM source_insight" in q:
                return insights
            if "full_text FROM source" in q:
                sid = str(params["id"])
                hit = next(s for s in sources if s["id"] == sid)
                return [{"full_text": hit["full_text"]}]
            if "FROM source" in q:
                return sources
            return []

        with (
            patch(
                "api.routers.processing.repo_query",
                new=AsyncMock(side_effect=fake_repo),
            ),
            patch("open_notebook.scheduler.submit.submit_held", side_effect=fake_submit),
            patch(
                "open_notebook.domain.transformation.Transformation.get",
                new=AsyncMock(return_value=MagicMock()),
            ),
        ):
            resp = client.post("/api/processing/ensure-insights", json={})

        assert resp.status_code == 200
        body = resp.json()
        # Only source:new lacks the insight; done has it, empty has no text.
        assert body["queued"] == 1
        assert submitted[0]["source_id"] == "source:new"
        assert body["skipped_have_insight"] == 1
        assert body["skipped_no_text"] == 1

    def test_idempotent_second_run_queues_nothing(self, client):
        sources = [_source("source:a", size=100)]
        transformations = [{"id": "transformation:t", "title": "Dense Summary"}]

        async def fake_repo(query, params=None):
            q = str(query)
            if "FROM transformation" in q and "VALUE" not in q:
                return transformations
            if "FROM source_insight" in q:
                # Second run sees what the first run queued... but queueing
                # does not create rows; simulate the created row instead.
                return [{"source": "source:a", "insight_type": "Dense Summary"}]
            if "full_text FROM source" in q:
                return [{"full_text": "text"}]
            if "FROM source" in q:
                return sources
            return []

        with (
            patch(
                "api.routers.processing.repo_query",
                new=AsyncMock(side_effect=fake_repo),
            ),
            patch(
                "open_notebook.scheduler.submit.submit_held",
                side_effect=AssertionError("must not submit"),
            ),
            patch(
                "open_notebook.domain.transformation.Transformation.get",
                new=AsyncMock(return_value=MagicMock()),
            ),
        ):
            resp = client.post("/api/processing/ensure-insights", json={})

        assert resp.json()["queued"] == 0
        assert resp.json()["skipped_have_insight"] == 1

    def test_skips_pairs_already_sitting_in_the_queue(self, client):
        """Deduplicating against the insight table alone is not enough.

        Queueing creates no rows, so a pair submitted twice has nothing
        to detect the duplicate: both jobs run, both complete, and the
        source ends up with twin insights. This is not theoretical - it
        is what happened when bulk ensure overlapped a manual resubmit,
        and the twins only showed up as a source with two Dense
        Summaries days later.
        """
        sources = [_source("source:queued"), _source("source:fresh")]
        transformations = [{"id": "transformation:t", "title": "Dense Summary"}]
        pending = [
            {
                "args": {
                    "source_id": "source:queued",
                    "transformation_id": "transformation:t",
                }
            }
        ]

        async def fake_repo(query, params=None):
            q = str(query)
            if "FROM transformation" in q and "VALUE" not in q:
                return transformations
            if "FROM source_insight" in q:
                return []
            if "FROM command" in q:
                return pending
            if "full_text FROM source" in q:
                return [{"full_text": "text"}]
            if "FROM source" in q:
                return sources
            return []

        submitted: List[dict] = []

        with (
            patch(
                "api.routers.processing.repo_query",
                new=AsyncMock(side_effect=fake_repo),
            ),
            patch(
                "open_notebook.scheduler.submit.submit_held",
                side_effect=_recording_submit(submitted),
            ),
            patch(
                "open_notebook.domain.transformation.Transformation.get",
                new=AsyncMock(return_value=MagicMock()),
            ),
        ):
            resp = client.post("/api/processing/ensure-insights", json={})

        assert resp.json()["queued"] == 1
        assert [a["source_id"] for a in submitted] == ["source:fresh"]

    def test_running_counts_as_pending_too(self, client):
        """A running job will produce the insight, and a second job for
        the same pair would produce a second one. `running` is not a
        gap to be filled.
        """
        sources = [_source("source:busy")]
        transformations = [{"id": "transformation:t", "title": "Key Insights"}]
        pending = [
            {
                "args": {
                    "source_id": "source:busy",
                    "transformation_id": "transformation:t",
                }
            }
        ]

        async def fake_repo(query, params=None):
            q = str(query)
            if "FROM transformation" in q and "VALUE" not in q:
                return transformations
            if "FROM source_insight" in q:
                return []
            if "FROM command" in q:
                return pending
            if "full_text FROM source" in q:
                return [{"full_text": "text"}]
            if "FROM source" in q:
                return sources
            return []

        with (
            patch(
                "api.routers.processing.repo_query",
                new=AsyncMock(side_effect=fake_repo),
            ),
            patch(
                "open_notebook.scheduler.submit.submit_held",
                side_effect=AssertionError("must not submit a second job"),
            ),
        ):
            resp = client.post("/api/processing/ensure-insights", json={})

        assert resp.json()["queued"] == 0
        assert resp.json()["skipped_have_insight"] == 1

    def test_pending_other_transformation_does_not_block(self, client):
        """The dedup key is (source, transformation). A pending Table of
        Contents must not make ensure skip the Dense Summary for the
        same source, or scoping a batch to two transformations would
        silently produce one.
        """
        sources = [_source("source:s")]
        transformations = [
            {"id": "transformation:a", "title": "Dense Summary"},
            {"id": "transformation:b", "title": "Key Insights"},
        ]
        pending = [
            {
                "args": {
                    "source_id": "source:s",
                    "transformation_id": "transformation:b",
                }
            }
        ]

        async def fake_repo(query, params=None):
            q = str(query)
            if "FROM transformation" in q and "VALUE" not in q:
                return transformations
            if "FROM source_insight" in q:
                return []
            if "FROM command" in q:
                return pending
            if "full_text FROM source" in q:
                return [{"full_text": "text"}]
            if "FROM source" in q:
                return sources
            return []

        submitted: List[dict] = []

        with (
            patch(
                "api.routers.processing.repo_query",
                new=AsyncMock(side_effect=fake_repo),
            ),
            patch(
                "open_notebook.scheduler.submit.submit_held",
                side_effect=_recording_submit(submitted),
            ),
            patch(
                "open_notebook.domain.transformation.Transformation.get",
                new=AsyncMock(return_value=MagicMock()),
            ),
        ):
            resp = client.post("/api/processing/ensure-insights", json={})

        assert resp.json()["queued"] == 1
        assert submitted[0]["transformation_id"] == "transformation:a"

    def test_unreadable_pending_row_does_not_sink_the_batch(self, client):
        """One malformed queue row must not cost every other pair: the
        scan is the only thing keeping the queue full.
        """
        sources = [_source("source:a")]
        transformations = [{"id": "transformation:t", "title": "Dense Summary"}]
        pending = [{"args": "not json at all"}, {"args": None}, {}]

        async def fake_repo(query, params=None):
            q = str(query)
            if "FROM transformation" in q and "VALUE" not in q:
                return transformations
            if "FROM source_insight" in q:
                return []
            if "FROM command" in q:
                return pending
            if "full_text FROM source" in q:
                return [{"full_text": "text"}]
            if "FROM source" in q:
                return sources
            return []

        submitted: List[dict] = []

        with (
            patch(
                "api.routers.processing.repo_query",
                new=AsyncMock(side_effect=fake_repo),
            ),
            patch(
                "open_notebook.scheduler.submit.submit_held",
                side_effect=_recording_submit(submitted),
            ),
            patch(
                "open_notebook.domain.transformation.Transformation.get",
                new=AsyncMock(return_value=MagicMock()),
            ),
        ):
            resp = client.post("/api/processing/ensure-insights", json={})

        assert resp.json()["queued"] == 1
        assert len(submitted) == 1


class TestRetryFailed:
    def test_clears_only_failed_extraction_jobs(self, client):
        updated = []

        async def fake_repo(query, params=None):
            q = str(query)
            if "SELECT id, command FROM source" in q:
                return [
                    {"id": "source:failed", "command": "command:f"},
                    {"id": "source:running", "command": "command:r"},
                    {"id": "source:done", "command": None},
                ]
            if "SET command = NONE" in q:
                updated.append(str(params["id"]))
                return []
            return []

        async def fake_skip(ref):
            return "needs a look" if ref == "command:f" else None

        with (
            patch(
                "api.routers.processing.repo_query",
                new=AsyncMock(side_effect=fake_repo),
            ),
            patch("open_notebook.sync.extraction._skip_reason", new=fake_skip),
        ):
            resp = client.post("/api/processing/retry-failed", json={})

        assert resp.status_code == 200
        assert resp.json()["cleared"] == ["source:failed"]


def _recording_submit(sink):
    """A submit_held stand-in that records args and returns an id.

    Not a lambda with `append(...) or "command:x"`: append returns None,
    so that idiom works only by accident and mypy rejects it. The point
    of these tests is to read like what they assert, not like a puzzle.
    """

    def submit(app, command, args):
        sink.append(args)
        return "command:recorded"

    return submit


class TestWindow:
    """The window is read fresh on every dispatcher pass.

    This class replaces one that tested a restart-based throttle: a
    persisted preference, a `restart_required` flag, and a second file
    recording what the worker had actually started with so the API could
    reconcile the two. Concurrency now lives upstream of the claim, so there
    is no reconciliation to get wrong - and correspondingly no field that
    could disagree with reality.
    """

    def test_persists_full_and_strict(self, client, tmp_path):
        env = tmp_path / "throttle.conf"
        with patch.dict("os.environ", {"THROTTLE_FILE": str(env)}):
            on = client.post("/api/processing/throttle", json={"enabled": True})
            assert on.status_code == 200
            assert on.json()["window"] == 3
            assert on.json()["restart_required"] is False
            assert env.read_text().strip() == "OPEN_NOTEBOOK_WORKER_MAX_TASKS=3"

            off = client.post("/api/processing/throttle", json={"enabled": False})
            assert off.json()["window"] == 1
            assert off.json()["enabled"] is False

    def test_a_toggle_needs_no_restart(self, client, tmp_path):
        """The requirement that superseded the old design. Asserted on the
        response and on the reader, because the flag alone would pass even if
        the dispatcher were still reading the value once at startup.
        """
        from open_notebook.scheduler.dispatcher import read_window

        env = tmp_path / "throttle.conf"
        with patch.dict("os.environ", {"THROTTLE_FILE": str(env)}):
            client.post("/api/processing/throttle", json={"enabled": True})
            # Read again, as a later dispatcher pass would.
            assert read_window() == 3
            client.post("/api/processing/throttle", json={"enabled": False})
            assert read_window() == 1

    def test_preserves_every_other_line_of_the_file(self, client, tmp_path):
        """The endpoint owns one key. Rewriting the whole file to store one
        integer would destroy the sync token sitting beside it.
        """
        env = tmp_path / ".env"
        env.write_text(
            "OPEN_NOTEBOOK_SYNC_TOKEN=secret\n"
            "OPEN_NOTEBOOK_WORKER_MAX_TASKS=9\n"
            "# a comment\n"
            "\n"
        )
        with patch.dict("os.environ", {"THROTTLE_FILE": str(env)}):
            resp = client.post("/api/processing/throttle", json={"enabled": True})

        assert resp.status_code == 200
        lines = env.read_text().splitlines()
        assert "OPEN_NOTEBOOK_SYNC_TOKEN=secret" in lines
        assert "# a comment" in lines
        assert [
            line for line in lines if line.startswith("OPEN_NOTEBOOK_WORKER_MAX_TASKS")
        ] == ["OPEN_NOTEBOOK_WORKER_MAX_TASKS=3"]

    def test_reader_honours_only_a_bare_value_in_range(self, tmp_path):
        """Anything the reader accepts, the dispatcher will actually use, and
        anything it rejects leaves the previous value in place rather than
        becoming a shell fragment.
        """
        from open_notebook.scheduler.dispatcher import read_window

        cases = {
            "OPEN_NOTEBOOK_WORKER_MAX_TASKS=3\n": 3,
            "OPEN_NOTEBOOK_WORKER_MAX_TASKS=1\n": 1,
            "OPEN_NOTEBOOK_WORKER_MAX_TASKS=8\n": 8,
            # 0 is meaningful here: it drains the queue and admits nothing.
            "OPEN_NOTEBOOK_WORKER_MAX_TASKS=0\n": 0,
            "OPEN_NOTEBOOK_WORKER_MAX_TASKS=9\n": 3,
            'OPEN_NOTEBOOK_WORKER_MAX_TASKS="3"\n': 3,
            "OPEN_NOTEBOOK_WORKER_MAX_TASKS=3 # fast\n": 3,
            "  OPEN_NOTEBOOK_WORKER_MAX_TASKS=3\n": 3,
            "OPEN_NOTEBOOK_WORKER_MAX_TASKS=3;rm -rf /\n": 3,
            "OPEN_NOTEBOOK_OTHER=3\n": 3,
            "": 3,
        }
        for content, expected in cases.items():
            path = tmp_path / "t.conf"
            path.write_text(content)
            with patch.dict("os.environ", {"THROTTLE_FILE": str(path)}):
                assert read_window() == expected, content

    def test_last_key_wins(self, tmp_path):
        from open_notebook.scheduler.dispatcher import read_window

        path = tmp_path / "t.conf"
        path.write_text(
            "OPEN_NOTEBOOK_WORKER_MAX_TASKS=3\nOPEN_NOTEBOOK_WORKER_MAX_TASKS=2\n"
        )
        with patch.dict("os.environ", {"THROTTLE_FILE": str(path)}):
            assert read_window() == 2

    def test_missing_file_falls_back_to_the_default(self, tmp_path):
        from open_notebook.scheduler.dispatcher import read_window

        with patch.dict("os.environ", {"THROTTLE_FILE": str(tmp_path / "absent")}):
            assert read_window() == 3

    def test_zero_window_is_a_legal_drain_setting(self, tmp_path):
        """Stopping admission without stopping anything already running."""
        from open_notebook.scheduler.dispatcher import read_window, write_window

        path = tmp_path / "t.conf"
        with patch.dict("os.environ", {"THROTTLE_FILE": str(path)}):
            assert write_window(0) == 0
            assert read_window() == 0

    def test_write_clamps_out_of_range_rather_than_storing_nonsense(self, tmp_path):
        from open_notebook.scheduler.dispatcher import read_window, write_window

        path = tmp_path / "t.conf"
        with patch.dict("os.environ", {"THROTTLE_FILE": str(path)}):
            assert write_window(99) == 8
            assert read_window() == 8
            assert write_window(-5) == 0
            assert read_window() == 0


class TestStatus:
    def test_reports_the_live_window_not_a_preference(self, client, tmp_path):
        rows = [
            {"status": "held", "n": 12},
            {"status": "new", "n": 1},
            {"status": "running", "n": 2},
            {"status": "failed", "n": 1},
        ]
        held = [{"held_reason": "memory", "n": 9}, {"held_reason": None, "n": 3}]

        async def fake_repo(query, params=None):
            q = str(query)
            if "GROUP BY status" in q:
                return rows
            if "GROUP BY held_reason" in q:
                return held
            return []

        env = tmp_path / "throttle.conf"
        env.write_text("OPEN_NOTEBOOK_WORKER_MAX_TASKS=3\n")
        with (
            patch(
                "api.routers.processing.repo_query",
                new=AsyncMock(side_effect=fake_repo),
            ),
            patch(
                "api.routers.processing.current_memory",
                return_value=MemoryReading(
                    total=15 * 1024**3,
                    available=8 * 1024**3,
                    free=7 * 1024**3,
                    readable=True,
                ),
            ),
            patch.dict("os.environ", {"THROTTLE_FILE": str(env)}),
        ):
            body = client.get("/api/processing/status").json()

        assert body["window"] == 3
        assert body["full_throttle"] is True
        # `new` and `running` together: the worker flips between them
        # asynchronously, so counting only `running` under-reports the load.
        assert body["in_flight"] == 3
        assert body["queue"]["held"] == 12
        assert body["held_reasons"]["memory"] == 9
        assert body["held_reasons"]["not-yet-considered"] == 3
        assert body["memory"]["readable"] is True
        assert body["memory"]["reserve"] > 0

    def test_memory_is_reported_as_unreadable_not_as_zero(self, client, tmp_path):
        """A zero available-memory figure reads as "this device has no
        memory", which is a different and much more alarming statement than
        "we could not tell".
        """

        async def fake_repo(query, params=None):
            return []

        with (
            patch(
                "api.routers.processing.repo_query",
                new=AsyncMock(side_effect=fake_repo),
            ),
            patch(
                "api.routers.processing.current_memory", return_value=MemoryReading()
            ),
            patch.dict("os.environ", {"THROTTLE_FILE": str(tmp_path / "absent")}),
        ):
            body = client.get("/api/processing/status").json()

        assert body["memory"]["readable"] is False
        assert body["memory"]["reserve"] == 0

    def test_status_survives_an_unreadable_queue(self, client, tmp_path):
        async def boom(query, params=None):
            raise RuntimeError("database is down")

        with (
            patch("api.routers.processing.repo_query", new=AsyncMock(side_effect=boom)),
            patch(
                "api.routers.processing.current_memory", return_value=MemoryReading()
            ),
            patch.dict("os.environ", {"THROTTLE_FILE": str(tmp_path / "absent")}),
        ):
            resp = client.get("/api/processing/status")

        assert resp.status_code == 200
        assert resp.json()["queue"] == {}

    def test_no_field_claims_a_restart_is_pending(self, client, tmp_path):
        """The fields that made the old design possible are gone, so the UI
        cannot render a badge that contradicts the dispatcher.
        """

        async def fake_repo(query, params=None):
            return []

        with (
            patch(
                "api.routers.processing.repo_query",
                new=AsyncMock(side_effect=fake_repo),
            ),
            patch(
                "api.routers.processing.current_memory", return_value=MemoryReading()
            ),
            patch.dict("os.environ", {"THROTTLE_FILE": str(tmp_path / "absent")}),
        ):
            body = client.get("/api/processing/status").json()

        for gone in ("throttle_restart_pending", "throttle_target", "worker_max_tasks"):
            assert gone not in body
