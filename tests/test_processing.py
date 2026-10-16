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


class TestStatus:
    def test_reports_concurrency_and_queue(self, client):
        rows = [
            {"status": "new", "n": 12},
            {"status": "running", "n": 2},
            {"status": "failed", "n": 1},
        ]
        with (
            patch(
                "api.routers.processing.repo_query",
                new=AsyncMock(
                    side_effect=[
                        rows,
                        [],
                    ]
                ),
            ),
            patch.dict("os.environ", {"OPEN_NOTEBOOK_WORKER_MAX_TASKS": "3"}),
        ):
            resp = client.get("/api/processing/status")

        assert resp.status_code == 200
        body = resp.json()
        assert body["worker_max_tasks"] == 3
        assert body["full_throttle"] is True
        assert body["queue"] == {"new": 12, "running": 2, "failed": 1}

    def test_defaults_to_single_worker(self, client):
        with (
            patch("api.routers.processing.repo_query", new=AsyncMock(return_value=[])),
            patch.dict("os.environ", {}, clear=False),
        ):
            import os

            os.environ.pop("OPEN_NOTEBOOK_WORKER_MAX_TASKS", None)
            resp = client.get("/api/processing/status")

        assert resp.json()["worker_max_tasks"] == 1
        assert resp.json()["full_throttle"] is False


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
            patch("surreal_commands.submit_command", side_effect=fake_submit),
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
                "surreal_commands.submit_command",
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
                "surreal_commands.submit_command",
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
                "surreal_commands.submit_command",
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
                "surreal_commands.submit_command",
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
                "surreal_commands.submit_command",
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
    """A submit_command stand-in that records args and returns an id.

    Not a lambda with `append(...) or "command:x"`: append returns None,
    so that idiom works only by accident and mypy rejects it. The point
    of these tests is to read like what they assert, not like a puzzle.
    """

    def submit(app, command, args):
        sink.append(args)
        return "command:recorded"

    return submit


class TestThrottle:
    """The throttle preference is a file both launchers grep, so the
    invariants are about what that file is allowed to say.

    Two failures are worth guarding and both are silent: a preference
    that reads back wrong (the toggle lies), and a preference the
    launchers cannot parse (the click lands, the restart ignores it).
    """

    def test_persists_full_and_strict(self, client, tmp_path):
        env = tmp_path / "throttle.env"
        with patch.dict("os.environ", {"THROTTLE_FILE": str(env)}):
            on = client.post("/api/processing/throttle", json={"enabled": True})
            assert on.status_code == 200
            assert on.json()["worker_max_tasks"] == 3
            assert on.json()["restart_required"] is True
            assert env.read_text().strip() == "OPEN_NOTEBOOK_WORKER_MAX_TASKS=3"

            off = client.post("/api/processing/throttle", json={"enabled": False})
            assert off.json()["worker_max_tasks"] == 1
            assert env.read_text().strip() == "OPEN_NOTEBOOK_WORKER_MAX_TASKS=1"

    def test_preserves_every_other_line_of_the_file(self, client, tmp_path):
        """The endpoint owns one key. Rewriting the whole file to store
        one integer would destroy the sync token sitting beside it -
        the same class of bug as a launcher sourcing what it greps.
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
        # exactly one throttle key, and it is the new value
        assert [
            line for line in lines if line.startswith("OPEN_NOTEBOOK_WORKER_MAX_TASKS")
        ] == ["OPEN_NOTEBOOK_WORKER_MAX_TASKS=3"]

    def test_reader_honours_only_a_bare_value_in_range(self, tmp_path):
        """What the launchers grep is `KEY=[1-8]` on its own line. A
        reader that accepts more than they do produces a preference
        that works in the UI and is silently ignored at startup.
        """
        from api.routers.processing import _read_throttle_tasks

        cases = {
            "OPEN_NOTEBOOK_WORKER_MAX_TASKS=3\n": 3,
            "OPEN_NOTEBOOK_WORKER_MAX_TASKS=1\n": 1,
            "OPEN_NOTEBOOK_WORKER_MAX_TASKS=8\n": 8,
            # out of the window, quoted, suffixed, indented by a shell,
            # or a fragment - all ignored, none of them executed
            "OPEN_NOTEBOOK_WORKER_MAX_TASKS=0\n": None,
            "OPEN_NOTEBOOK_WORKER_MAX_TASKS=9\n": None,
            'OPEN_NOTEBOOK_WORKER_MAX_TASKS="3"\n': None,
            "OPEN_NOTEBOOK_WORKER_MAX_TASKS=3 # fast\n": None,
            "  OPEN_NOTEBOOK_WORKER_MAX_TASKS=3\n": None,
            "OPEN_NOTEBOOK_WORKER_MAX_TASKS=3;rm -rf /\n": None,
            "OPEN_NOTEBOOK_OTHER=3\n": None,
            "": None,
        }
        for content, expected in cases.items():
            path = tmp_path / "t.env"
            path.write_text(content)
            with patch.dict("os.environ", {"THROTTLE_FILE": str(path)}):
                assert _read_throttle_tasks() == expected, content

    def test_last_key_wins(self, tmp_path):
        """Two keys can only exist if something appended badly. The
        launchers take the last match, so the reader must agree or the
        two disagree about which is the preference.
        """
        from api.routers.processing import _read_throttle_tasks

        path = tmp_path / "t.env"
        path.write_text(
            "OPEN_NOTEBOOK_WORKER_MAX_TASKS=3\nOPEN_NOTEBOOK_WORKER_MAX_TASKS=2\n"
        )
        with patch.dict("os.environ", {"THROTTLE_FILE": str(path)}):
            assert _read_throttle_tasks() == 2

    def test_worker_record_beats_the_environment(self, tmp_path):
        """The launcher's entire job is to override the environment.

        So reading the environment to describe the *running* worker
        reports the number that was overridden: a worker genuinely
        started at 3 gets described as 1, and a restart that already
        applied the change is reported as still pending. The launcher's
        own record of what it started with is the only honest source.
        """
        from api.routers.processing import _worker_max_tasks

        env = tmp_path / "throttle.env"
        env.write_text("OPEN_NOTEBOOK_WORKER_MAX_TASKS=3\n")
        running = tmp_path / "throttle.running"
        running.write_text("OPEN_NOTEBOOK_WORKER_MAX_TASKS=3\n")
        with patch.dict(
            "os.environ",
            {"THROTTLE_FILE": str(env), "OPEN_NOTEBOOK_WORKER_MAX_TASKS": "1"},
        ):
            assert _worker_max_tasks() == 3

    def test_environment_is_the_fallback_before_any_worker_starts(self, tmp_path):
        """A fresh container has no throttle.running yet. Falling back
        to the environment is right there and only there.
        """
        from api.routers.processing import _worker_max_tasks

        env = tmp_path / "throttle.env"
        env.write_text("OPEN_NOTEBOOK_WORKER_MAX_TASKS=3\n")
        with patch.dict(
            "os.environ",
            {"THROTTLE_FILE": str(env), "OPEN_NOTEBOOK_WORKER_MAX_TASKS": "2"},
        ):
            assert _worker_max_tasks() == 2

    def test_settled_after_restart_is_not_pending(self, client, tmp_path):
        """The end state of the whole feature: the click is written, the
        restart happened, the worker is at 3 - and nothing claims to be
        waiting on anything.
        """
        env = tmp_path / "throttle.env"
        env.write_text("OPEN_NOTEBOOK_WORKER_MAX_TASKS=3\n")
        running = tmp_path / "throttle.running"
        running.write_text("OPEN_NOTEBOOK_WORKER_MAX_TASKS=3\n")
        with (
            patch(
                "api.routers.processing.repo_query",
                new=AsyncMock(return_value=[]),
            ),
            patch.dict(
                "os.environ",
                # the environment still says 2; the worker overrode it
                {"THROTTLE_FILE": str(env), "OPEN_NOTEBOOK_WORKER_MAX_TASKS": "2"},
            ),
        ):
            body = client.get("/api/processing/status").json()

        assert body["worker_max_tasks"] == 3
        assert body["full_throttle"] is True
        assert body["throttle_restart_pending"] is False

    def test_status_separates_running_worker_from_saved_preference(
        self, client, tmp_path
    ):
        """The click lands before the restart does. Reporting only the
        live worker makes the toggle look dead; reporting only the
        preference makes the device look faster than it is. Both, plus
        an explicit pending flag, so neither is a guess.
        """
        env = tmp_path / "throttle.env"
        env.write_text("OPEN_NOTEBOOK_WORKER_MAX_TASKS=3\n")
        with (
            patch(
                "api.routers.processing.repo_query",
                new=AsyncMock(return_value=[]),
            ),
            patch.dict(
                "os.environ",
                {"THROTTLE_FILE": str(env), "OPEN_NOTEBOOK_WORKER_MAX_TASKS": "1"},
            ),
        ):
            body = client.get("/api/processing/status").json()

        assert body["worker_max_tasks"] == 1  # what is true now
        assert body["full_throttle"] is False
        assert body["throttle_target"] == 3  # what the restart will use
        assert body["throttle_restart_pending"] is True

    def test_status_quiet_when_nothing_pending(self, client, tmp_path):
        env = tmp_path / "throttle.env"
        env.write_text("OPEN_NOTEBOOK_WORKER_MAX_TASKS=3\n")
        with (
            patch(
                "api.routers.processing.repo_query",
                new=AsyncMock(return_value=[]),
            ),
            patch.dict(
                "os.environ",
                {"THROTTLE_FILE": str(env), "OPEN_NOTEBOOK_WORKER_MAX_TASKS": "3"},
            ),
        ):
            body = client.get("/api/processing/status").json()

        assert body["throttle_restart_pending"] is False
        assert body["throttle_target"] == 3

    def test_missing_file_falls_back_to_the_running_value(self, client, tmp_path):
        with (
            patch(
                "api.routers.processing.repo_query",
                new=AsyncMock(return_value=[]),
            ),
            patch.dict(
                "os.environ",
                {
                    "THROTTLE_FILE": str(tmp_path / "absent.env"),
                    "OPEN_NOTEBOOK_WORKER_MAX_TASKS": "2",
                },
            ),
        ):
            body = client.get("/api/processing/status").json()

        assert body["throttle_target"] == 2
        assert body["throttle_restart_pending"] is False
