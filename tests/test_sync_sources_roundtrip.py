"""Source manifest round-trip against a real SurrealDB.

Why this file exists and why it needs a real server
---------------------------------------------------
Every bug found so far in this area was invisible to the stubbed suite:

* `fsrs_json` silently discarded on a SCHEMAFULL field lacking FLEXIBLE
  - a SurrealDB schema behaviour, not application logic;
* `ORDER BY created` with `created` unselected - a SurrealQL *parse*
  error;
* `id != $exclude` matching nothing when the parameter was a string -
  type coercion, invisible unless you count real rows.

A mocked `repo_query` agrees with every one of those bugs. So these
tests run against a live database, on throwaway namespaces, and skip
when one is not reachable rather than silently passing.

    SURREAL_TEST_URL=ws://127.0.0.1:8108/rpc \\
    SURREAL_TEST_USER=root SURREAL_TEST_PASSWORD=root \\
        pytest tests/test_sync_sources_roundtrip.py

Two namespaces on one server stand in for two devices. Reusing a single
namespace would make the import see its own export and skip everything
as stale - passing while testing nothing.

What is asserted is the property that matters, not the mechanics: after
export on A and import on B, B knows the source exists, by title and by
hash, and does *not* pretend to have a file it never received.
"""

import os
import uuid
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    not os.getenv("SURREAL_TEST_URL"),
    reason="needs a live SurrealDB; set SURREAL_TEST_URL",
)


@pytest.fixture
def use_device(monkeypatch, tmp_path):
    """Point the app at throwaway namespaces, one per simulated device.

    The repository reads configuration straight from the environment -
    ``db_connection()`` calls ``get_database_url()`` and friends - so
    namespaces are swapped via env vars rather than by patching a
    settings object that does not exist.

    Local sync state (the baseline) moves with the device for the same
    reason: it lives under DATA_FOLDER, so each simulated device gets
    its own, the way real devices have their own data directories.
    """
    issued: list[str] = []
    current = {"ns": ""}

    def use(label: str) -> str:
        ns = f"synctest_{label}_{uuid.uuid4().hex[:8]}"
        issued.append(ns)
        monkeypatch.setenv("SURREAL_NAMESPACE", ns)
        monkeypatch.setenv("SURREAL_DATABASE", ns)
        monkeypatch.setenv("DATA_FOLDER", str(tmp_path / f"data_{label}"))
        current["ns"] = ns
        return ns

    monkeypatch.setenv("SURREAL_URL", os.environ["SURREAL_TEST_URL"])
    monkeypatch.setenv("SURREAL_USER", os.getenv("SURREAL_TEST_USER", "root"))
    monkeypatch.setenv("SURREAL_PASSWORD", os.getenv("SURREAL_TEST_PASSWORD", "root"))
    use("a")

    yield use

    # Best-effort cleanup. SurrealDB 2.x silently ignores REMOVE DATABASE,
    # so records are deleted explicitly instead.
    import asyncio

    from open_notebook.database.repository import repo_query

    async def wipe_all() -> None:
        for ns in issued:
            monkeypatch.setenv("SURREAL_NAMESPACE", ns)
            monkeypatch.setenv("SURREAL_DATABASE", ns)
            for table in (
                "source",
                "note",
                "flashcard",
                "revlog",
                "artifact",
                "notebook",
            ):
                try:
                    await repo_query(f"DELETE {table}")
                except Exception:
                    pass

    try:
        asyncio.run(wipe_all())
    except Exception:
        pass


@pytest.fixture
def blobs(monkeypatch, tmp_path: Path):
    """Point the blob store at a per-device directory: ``blobs("a")``.

    Sharing one root would let device B resolve a hash that only device
    A ever stored, which is exactly the thing under test - so the two
    devices must have genuinely separate stores.

    Patched at the module's own ``_root`` rather than the config
    constant: ``blobs._root`` imports ``BLOBS_FOLDER`` lazily on every
    call, so patching the constant would not affect an already-resolved
    reference.
    """
    import open_notebook.storage.blobs as blobmod

    def use(device: str) -> Path:
        root = tmp_path / f"blobs-{device}"
        monkeypatch.setattr(
            blobmod,
            "_root",
            lambda override=None: root if override is None else Path(override),
        )
        return root

    return use


class TestSourceManifestRoundTrip:
    @pytest.mark.asyncio
    async def test_source_crosses_devices_without_its_bytes(
        self, use_device, blobs, tmp_path
    ):
        """A exports a file source; B gets the metadata, not the file.

        This is the whole point of the manifest split. B must know the
        source exists and how to ask for its content, while being
        truthful that it cannot open it yet.
        """
        from open_notebook.domain.notebook import Asset, Notebook, Source
        from open_notebook.storage.blobs import store_bytes
        from open_notebook.sync.exchange import SOURCES_DIR, export_all, import_all

        sync = tmp_path / "sync"

        # --- device A ---
        blobs("a")
        ref = store_bytes(b"lecture pdf bytes", "Week 1.pdf")
        src = Source(
            title="Week 1 lecture",
            topics=["histology"],
            asset=Asset(
                file_path=ref.path,
                sha256=ref.sha256,
                filename="Week 1.pdf",
                size=ref.size,
            ),
        )
        await src.save()
        nb = Notebook(name="GTB221 - Basic Hematology", description="course notebook")
        await nb.save()
        await src.add_to_notebook(str(nb.id))

        manifest = await export_all("device-a", root=sync)

        assert manifest["counts"]["sources"] == 1
        entry = next((sync / SOURCES_DIR).glob("*.json")).read_text()
        assert ref.sha256 in entry, "the manifest must carry the content hash"
        assert "Week 1 lecture" in entry
        assert "file_path" not in entry, (
            "a path on the sending device must never reach the manifest"
        )
        leaked = [
            p for p in sync.rglob("*") if p.is_file() and p.suffix in (".pdf", ".epub")
        ]
        assert not leaked, f"source bytes leaked into the sync dir: {leaked}"

        # --- device B: same repo, separate database, no bytes ---
        # A real import, not a dry run: the notebook-attachment loop runs
        # after the dry-run `continue`, so missing_notebooks can only be
        # observed on a real pass.
        use_device("b")
        blobs("b")
        result = await import_all(root=sync)
        assert result["sources_created"] == 1, result
        assert result["sources_awaiting_blobs"] == 1, (
            "B must be told it is missing the bytes"
        )
        assert result["missing_notebooks"] == [], (
            "the notebook arrived in the same manifest, so the slug must "
            "resolve - it used to miss because the notebook map was "
            "snapshotted before notebooks were imported"
        )
        from open_notebook.database.repository import ensure_record_id, repo_query

        [notebook] = await repo_query("SELECT VALUE id FROM notebook")
        edges = await repo_query(
            "SELECT count() AS c FROM reference WHERE out = $id GROUP ALL",
            {"id": ensure_record_id(str(notebook))},
        )
        assert edges[0]["c"] == 1, "the source must be filed, not just present"

    @pytest.mark.asyncio
    async def test_import_creates_source_without_a_file_path(
        self, use_device, blobs, tmp_path
    ):
        """The receiving device must never inherit a foreign path.

        Copying it would produce a record pointing at a file that does
        not exist here - which reads as "present" to every existence
        check until something tries to read it.
        """
        from open_notebook.domain.notebook import Asset, Source
        from open_notebook.storage.blobs import store_bytes
        from open_notebook.sync.exchange import export_all, import_all

        sync = tmp_path / "sync"
        blobs("a")
        ref = store_bytes(b"foreign path bytes", "x.pdf")
        src = Source(
            title="Has a path",
            asset=Asset(
                file_path=ref.path,
                sha256=ref.sha256,
                filename="x.pdf",
                size=ref.size,
            ),
        )
        await src.save()
        record_id = str(src.id)
        await export_all("a", root=sync)

        use_device("b")
        blobs("b")
        await import_all(root=sync)
        copied = await Source.get(record_id)
        assert copied.asset is not None
        assert copied.asset.sha256 == ref.sha256
        assert copied.asset.file_path is None, (
            "import must not copy the sending device's path"
        )
        assert copied.has_local_file is False
        assert copied.local_file is None

    @pytest.mark.asyncio
    async def test_local_file_resolves_once_the_blob_arrives(
        self, use_device, blobs, tmp_path
    ):
        """After a blob transfer, the source becomes usable with no re-sync.

        The path is derived from the hash on every access, so receiving
        bytes is enough - nothing has to rewrite the record.
        """
        from open_notebook.domain.notebook import Asset, Source
        from open_notebook.storage.blobs import store_bytes
        from open_notebook.sync.exchange import export_all, import_all

        sync = tmp_path / "sync"
        blobs("a")
        payload = b"arrives later bytes"
        ref = store_bytes(payload, "later.pdf")
        src = Source(
            title="Later",
            asset=Asset(
                file_path=ref.path,
                sha256=ref.sha256,
                filename="later.pdf",
                size=ref.size,
            ),
        )
        await src.save()
        record_id = str(src.id)
        await export_all("a", root=sync)

        use_device("b")
        blobs("b")
        await import_all(root=sync)
        imported = await Source.get(record_id)
        assert imported.has_local_file is False

        # The blob channel delivers the bytes. No re-import needed.
        store_bytes(payload, "later.pdf")
        assert imported.has_local_file is True
        assert Path(imported.local_file).read_bytes() == payload

    @pytest.mark.asyncio
    async def test_export_is_idempotent(self, use_device, blobs, tmp_path):
        """A second cycle must not churn the repo.

        Byte-identical output means git has nothing to commit, which is
        what stops every cycle producing a commit.
        """
        from open_notebook.domain.notebook import Asset, Source
        from open_notebook.storage.blobs import store_bytes
        from open_notebook.sync.exchange import export_all

        sync = tmp_path / "sync"
        blobs("a")
        ref = store_bytes(b"idempotent bytes", "i.pdf")
        src = Source(
            title="Idem",
            topics=["x"],
            asset=Asset(
                file_path=ref.path,
                sha256=ref.sha256,
                filename="i.pdf",
                size=ref.size,
            ),
        )
        await src.save()

        def snapshot() -> dict[str, bytes]:
            return {
                str(p.relative_to(sync)): p.read_bytes()
                for p in sorted(sync.rglob("*"))
                if p.is_file()
            }

        await export_all("a", root=sync)
        first = snapshot()
        await export_all("a", root=sync)
        second = snapshot()

        assert set(first) == set(second)
        differing = [k for k in first if first[k] != second[k]]
        assert not differing, f"export is not idempotent: {differing}"


class TestSourceKinds:
    @pytest.mark.asyncio
    async def test_link_source_does_not_wait_for_bytes(self):
        """A link is fully described by its manifest entry.

        Counting it as awaiting a blob would be a permanent false alarm
        in every sync report.
        """
        from open_notebook.domain.notebook import Asset
        from open_notebook.sync.exchange import _source_kind

        assert _source_kind(Asset(url="https://example.com/x")) == "link"
        assert _source_kind(Asset(sha256="a" * 64)) == "file"
        assert _source_kind(Asset(file_path="/tmp/x.pdf")) == "file"
        assert _source_kind(Asset()) == "text"


class TestBackwardCompatibility:
    @pytest.mark.asyncio
    async def test_v1_repo_without_sources_imports_cleanly(
        self, use_device, blobs, tmp_path
    ):
        """An old repo has no sources/ directory.

        It must import as 'no sources', not as an error, so a phone that
        has not updated yet does not break on a repo written by a build
        that has.
        """
        from open_notebook.sync.exchange import import_all

        sync = tmp_path / "old-sync"
        (sync / "notes").mkdir(parents=True)
        (sync / "manifest.json").write_text(
            '{"schema_version": 1, "device": "old", "counts": {}}'
        )
        use_device("b")
        blobs("b")
        result = await import_all(root=sync, dry_run=True)
        assert result["sources_created"] == 0
        assert result["errors"] == []
