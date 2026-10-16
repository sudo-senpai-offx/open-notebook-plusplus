"""Processing control: throttle visibility, scheduled ensure, explicit retry.

Bulk insight generation must be idempotent (re-running after an
interruption fills gaps, never duplicates), ordered smallest-first (so
results land early and problem files surface first), and honest about
what it skips. Retries must touch only failed/canceled extractions,
never live jobs or finished work.
"""

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
        submitted = []

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
