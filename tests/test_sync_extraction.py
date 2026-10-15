"""Extraction follows blob arrival, not just upload.

A synced source arrives as metadata first and bytes later. Import
creates it with no `full_text`, the blob channel delivers the file, and
without a hook connecting the two the library reports every hash
resolved while every file opens as "No content available" - which is
exactly what the phone showed.

`enqueue_missing_extraction` is that hook: it submits the same
`process_source` job an upload submits for every source that has a
resolvable file but no text and no command. Import calls it on every
real pass; the blob pull endpoint calls it after fetching.
"""

import os
import uuid

import pytest

pytestmark = pytest.mark.skipif(
    not os.getenv("SURREAL_TEST_URL"),
    reason="needs a live SurrealDB; set SURREAL_TEST_URL",
)


@pytest.fixture
def device(monkeypatch, tmp_path):
    """An isolated namespace with its own blob store and a fake queue."""
    import open_notebook.config as config

    ns = f"extract_{uuid.uuid4().hex[:8]}"
    monkeypatch.setenv("SURREAL_NAMESPACE", ns)
    monkeypatch.setenv("SURREAL_DATABASE", ns)
    monkeypatch.setenv("SURREAL_URL", os.environ["SURREAL_TEST_URL"])
    monkeypatch.setenv("SURREAL_USER", os.getenv("SURREAL_TEST_USER", "root"))
    monkeypatch.setenv("SURREAL_PASSWORD", os.getenv("SURREAL_TEST_PASSWORD", "root"))

    blobs = tmp_path / "blobs"
    blobs.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(config, "BLOBS_FOLDER", str(blobs), raising=False)
    # Local sync state moves with the device too: the export writes the
    # baseline into DATA_FOLDER, and a second namespace sharing it would
    # read the first one's exports as its own memory - tripping the
    # resurrection gate on records it never saw. Same isolation the
    # other sync fixtures already do.
    monkeypatch.setenv("DATA_FOLDER", str(tmp_path / "data_a"))

    submitted: list[dict] = []

    async def fake_submit(app: str, command: str, args: dict) -> str:
        submitted.append({"app": app, "command": command, "args": args})
        return f"command:{uuid.uuid4().hex[:8]}"

    from api.command_service import CommandService

    monkeypatch.setattr(CommandService, "submit_command_job", fake_submit)
    return submitted


async def _source(title: str, **asset_fields):
    from open_notebook.domain.notebook import Asset, Source

    source = Source(title=title, topics=[], asset=Asset(**asset_fields))
    await source.save()
    return source


class TestTheScan:
    @pytest.mark.asyncio
    async def test_a_file_without_text_is_queued(self, device):
        from open_notebook.storage.blobs import store_bytes
        from open_notebook.sync.extraction import enqueue_missing_extraction

        ref = store_bytes(b"extract me", "paper.pdf")
        source = await _source("Unprocessed", sha256=ref.sha256, filename="paper.pdf")

        result = await enqueue_missing_extraction()

        assert result["queued_count"] == 1
        assert result["queued"] == [str(source.id)]
        assert len(device) == 1
        assert device[0]["command"] == "process_source"
        assert device[0]["args"]["source_id"] == str(source.id)
        assert device[0]["args"]["embed"] is True

    @pytest.mark.asyncio
    async def test_a_source_with_text_is_left_alone(self, device):
        from open_notebook.storage.blobs import store_bytes
        from open_notebook.sync.extraction import enqueue_missing_extraction

        ref = store_bytes(b"already done", "done.pdf")
        source = await _source("Processed", sha256=ref.sha256, filename="done.pdf")
        source.full_text = "extracted long ago"
        await source.save()

        result = await enqueue_missing_extraction()

        assert result["queued_count"] == 0
        assert result["skipped_have_text"] == 1
        assert device == []

    @pytest.mark.asyncio
    async def test_a_live_job_is_left_alone(self, device):
        """Re-queueing over a running job doubles the work."""
        from open_notebook.database.repository import ensure_record_id, repo_query
        from open_notebook.storage.blobs import store_bytes
        from open_notebook.sync.extraction import enqueue_missing_extraction

        ref = store_bytes(b"in flight", "flight.pdf")
        source = await _source("In flight", sha256=ref.sha256, filename="flight.pdf")
        await repo_query(
            "CREATE $cmd CONTENT { name: 'process_source', status: 'running' }",
            {"cmd": ensure_record_id(f"command:{uuid.uuid4().hex[:8]}")},
        )
        job = (
            await repo_query("SELECT VALUE id FROM command ORDER BY id DESC LIMIT 1")
        )[0]
        await repo_query(
            "UPDATE $id SET command = $cmd",
            {"id": ensure_record_id(str(source.id)), "cmd": ensure_record_id(str(job))},
        )

        result = await enqueue_missing_extraction()

        assert result["queued_count"] == 0
        assert result["skipped_have_job"] == 1
        assert result["needs_retry"] == []

    @pytest.mark.asyncio
    async def test_a_failed_job_needs_eyes_not_a_requeue(self, device):
        """A failure is an explicit signal; silently looping it would burn
        a phone's CPU on a file that cannot be read."""
        from open_notebook.database.repository import ensure_record_id, repo_query
        from open_notebook.storage.blobs import store_bytes
        from open_notebook.sync.extraction import enqueue_missing_extraction

        ref = store_bytes(b"doomed", "doomed.pdf")
        source = await _source("Doomed", sha256=ref.sha256, filename="doomed.pdf")
        await repo_query(
            "CREATE $cmd CONTENT { name: 'process_source', status: 'failed' }",
            {"cmd": ensure_record_id(f"command:{uuid.uuid4().hex[:8]}")},
        )
        job = (
            await repo_query("SELECT VALUE id FROM command ORDER BY id DESC LIMIT 1")
        )[0]
        await repo_query(
            "UPDATE $id SET command = $cmd",
            {"id": ensure_record_id(str(source.id)), "cmd": ensure_record_id(str(job))},
        )

        result = await enqueue_missing_extraction()

        assert result["queued_count"] == 0
        assert result["needs_retry"] == [str(source.id)]

    @pytest.mark.asyncio
    async def test_a_pruned_job_record_does_not_strand_its_file(self, device):
        """Retention eats old command records. A reference pointing at a
        missing record is old news, not a live job - the file must queue
        again without anyone clearing anything by hand."""
        from open_notebook.database.repository import ensure_record_id, repo_query
        from open_notebook.storage.blobs import store_bytes
        from open_notebook.sync.extraction import enqueue_missing_extraction

        ref = store_bytes(b"orphaned", "orphaned.pdf")
        source = await _source("Orphaned", sha256=ref.sha256, filename="orphaned.pdf")
        await repo_query(
            "UPDATE $id SET command = $cmd",
            {
                "id": ensure_record_id(str(source.id)),
                "cmd": ensure_record_id(f"command:{uuid.uuid4().hex[:8]}"),
            },
        )

        result = await enqueue_missing_extraction()

        assert result["queued_count"] == 1
        assert result["queued"] == [str(source.id)]

    @pytest.mark.asyncio
    async def test_a_hash_without_bytes_is_left_alone(self, device):
        """The bytes have not arrived yet; queueing now would process
        nothing, and the next import after they land queues then."""
        from open_notebook.sync.extraction import enqueue_missing_extraction

        await _source("Metadata only", sha256="a" * 64, filename="ghost.pdf")

        result = await enqueue_missing_extraction()

        assert result["queued_count"] == 0
        assert result["skipped_no_file"] == 1

    @pytest.mark.asyncio
    async def test_a_link_source_is_left_alone(self, device):
        from open_notebook.sync.extraction import enqueue_missing_extraction

        await _source("Link", url="https://example.com/paper")

        result = await enqueue_missing_extraction()

        assert result["queued_count"] == 0

    @pytest.mark.asyncio
    async def test_one_bad_record_strands_nothing(self, device):
        from open_notebook.storage.blobs import store_bytes
        from open_notebook.sync.extraction import enqueue_missing_extraction

        ref = store_bytes(b"good", "good.pdf")
        await _source("Good", sha256=ref.sha256, filename="good.pdf")
        await _source("Bad", sha256="z" * 64, filename="bad.pdf")

        result = await enqueue_missing_extraction()

        assert result["queued_count"] == 1


class TestTheImportHook:
    @pytest.mark.asyncio
    async def test_import_queues_completed_sources(self, device, tmp_path):
        """The phone's case: metadata imported, bytes present, no text."""
        from open_notebook.storage.blobs import store_bytes
        from open_notebook.sync.exchange import export_all, import_all

        ref = store_bytes(b"phone paper", "phone.pdf")
        await _source("Phone paper", sha256=ref.sha256, filename="phone.pdf")
        sync = tmp_path / "sync"
        await export_all(device="pc", root=sync)

        # A second namespace stands in for the phone: same manifest,
        # bytes copied over like the blob channel would.
        import open_notebook.config as config

        phone_blobs = tmp_path / "phone_blobs"
        phone_blobs.mkdir(parents=True, exist_ok=True)
        config.BLOBS_FOLDER = str(phone_blobs)
        import shutil

        for shard in (tmp_path / "blobs").iterdir():
            if shard.is_dir():
                target = phone_blobs / shard.name
                target.mkdir(parents=True, exist_ok=True)
                for entry in shard.iterdir():
                    shutil.copy(entry, target / entry.name)

        import os as _os

        _os.environ["SURREAL_NAMESPACE"] = f"extract_phone_{uuid.uuid4().hex[:8]}"
        _os.environ["SURREAL_DATABASE"] = _os.environ["SURREAL_NAMESPACE"]
        _os.environ["DATA_FOLDER"] = str(tmp_path / "data_b")

        result = await import_all(root=sync)

        assert result["sources_created"] == 1
        assert result["sources_queued_for_extraction"] == 1
