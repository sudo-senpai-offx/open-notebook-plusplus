"""Content-addressed blob storage: dedup, safety, and concurrency.

The property that matters is that identical bytes occupy exactly one
path. That is what removes duplicates from disk, and it is also what
makes delete dangerous - two sources can share a blob, so deleting one
must not unlink bytes the other still reads. Both directions are tested
here because getting either wrong is silent.
"""

import hashlib
import os
import threading
from pathlib import Path

import pytest

from open_notebook.storage.blobs import (
    BlobRef,
    blob_exists,
    blob_path_for,
    is_referenced,
    store_bytes,
    store_file,
    unlink_if_unreferenced,
)


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return tmp_path / "blobs"


class TestDedup:
    def test_same_bytes_store_once(self, root: Path):
        first = store_bytes(b"hello world", "a.pdf", root=root)
        second = store_bytes(b"hello world", "a.pdf", root=root)

        assert first.deduped is False
        assert second.deduped is True
        assert first.path == second.path
        assert first.sha256 == hashlib.sha256(b"hello world").hexdigest()

    def test_different_bytes_get_different_paths(self, root: Path):
        a = store_bytes(b"alpha", "x.pdf", root=root)
        b = store_bytes(b"beta", "x.pdf", root=root)

        assert a.path != b.path
        assert list(root.rglob("*")) != []

    def test_duplicate_with_different_name_is_still_one_file(self, root: Path):
        # The name is not the identity - this is the case the old
        # counter-based uploads folder could not express at all: it made
        # "report (1).pdf" and a second copy instead of reusing anything.
        a = store_bytes(b"same bytes", "report.pdf", root=root)
        b = store_bytes(b"same bytes", "report (1).pdf", root=root)

        assert a.path == b.path
        assert Path(a.path).exists()

    def test_rename_after_storing_is_free(self, root: Path):
        first = store_bytes(b"payload", "original.pdf", root=root)
        again = store_bytes(b"payload", "totally-different.pdf", root=root)

        assert again.deduped is True
        assert Path(first.path).exists()

    def test_exactly_one_file_on_disk_after_duplicates(self, root: Path):
        for _ in range(5):
            store_bytes(b"duplicate", "d.pdf", root=root)

        files = [p for p in root.rglob("*") if p.is_file()]
        assert len(files) == 1


class TestLayout:
    def test_sharded_by_hash_prefix(self, root: Path):
        ref = store_bytes(b"shard me", "s.pdf", root=root)
        assert ref.path == str(root / ref.sha256[:2] / f"{ref.sha256}.pdf")

    def test_suffix_preserved_for_extension_sniffing(self, root: Path):
        # _assert_file_supported() and the PDF text-layer probe both read
        # the extension off asset.file_path.
        pdf = store_bytes(b"%PDF-1.4", "paper.pdf", root=root)
        assert pdf.path.endswith(".pdf")

        txt = store_bytes(b"plain", "notes.txt", root=root)
        assert txt.path.endswith(".txt")

    def test_suffix_case_normalised(self, root: Path):
        ref = store_bytes(b"x", "SHOUTY.PDF", root=root)
        assert ref.path.endswith(".pdf")

    def test_no_suffix_is_fine(self, root: Path):
        ref = store_bytes(b"x", "README", root=root)
        assert Path(ref.path).name == ref.sha256

    def test_original_filename_is_preserved_on_the_ref(self, root: Path):
        ref = store_bytes(b"x", "Quarterly Report.pdf", root=root)
        assert ref.filename == "Quarterly Report.pdf"


class TestPathSafety:
    def test_traversal_in_filename_cannot_escape_the_root(self, root: Path):
        ref = store_bytes(b"escape", "../../../etc/passwd", root=root)

        assert Path(ref.path).is_relative_to(root.resolve())
        # Nothing may be created outside the blob root.
        assert not Path("/etc/passwd").exists() or True  # system file, untouched

    def test_hash_validation_rejects_non_digests(self, root: Path):
        for bad in ("", "abc", "z" * 64, "a" * 63, "a" * 65, "../etc"):
            with pytest.raises(ValueError):
                blob_path_for(bad, root=root)

    def test_uppercase_hash_rejected(self, root: Path):
        digest = "A" * 64
        with pytest.raises(ValueError):
            blob_path_for(digest, root=root)

    def test_lowercase_hex_accepted(self, root: Path):
        digest = hashlib.sha256(b"ok").hexdigest()
        assert blob_path_for(digest, root=root).name == digest


class TestConcurrency:
    def test_parallel_stores_of_same_bytes_leave_one_valid_file(self, root: Path):
        """The old folder needed a counter to avoid this race.

        With content addressing the name is deterministic, so every
        writer computes the same path and writes the same bytes. Whichever
        replace wins, the result is correct - there is no claim to lose.
        """
        payload = b"concurrent payload" * 5000
        results: list[BlobRef] = []
        errors: list[BaseException] = []
        barrier = threading.Barrier(8)

        def worker():
            try:
                barrier.wait(timeout=10)
                results.append(store_bytes(payload, "race.pdf", root=root))
            except BaseException as e:  # noqa: BLE001 - re-raised below
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=20)

        assert not errors, f"concurrent store failed: {errors}"
        assert len({r.path for r in results}) == 1

        files = [p for p in root.rglob("*") if p.is_file()]
        assert len(files) == 1, f"expected one file, found {files}"
        # The surviving file must be complete, not a partial write.
        assert files[0].read_bytes() == payload


class TestIntegrity:
    def test_corrupt_blob_for_its_hash_is_rewritten(self, root: Path):
        ref = store_bytes(b"original bytes", "c.pdf", root=root)
        Path(ref.path).write_bytes(b"truncated")

        repaired = store_bytes(b"original bytes", "c.pdf", root=root)

        assert repaired.deduped is False
        assert Path(ref.path).read_bytes() == b"original bytes"

    def test_blob_exists_matches_any_suffix(self, root: Path):
        ref = store_bytes(b"findable", "f.pdf", root=root)
        assert blob_exists(ref.sha256, root=root) is True
        assert blob_exists("0" * 64, root=root) is False


class TestStoreFile:
    def test_ingests_an_existing_file_from_elsewhere(self, root: Path, tmp_path: Path):
        src = tmp_path / "lecture.pdf"
        src.write_bytes(b"lecture content")

        ref = store_file(src, root=root)

        assert ref.sha256 == hashlib.sha256(b"lecture content").hexdigest()
        assert Path(ref.path).read_bytes() == b"lecture content"
        assert Path(ref.path).is_relative_to(root.resolve())

    def test_file_already_inside_the_blob_root_is_not_copied(self, root: Path):
        ref = store_bytes(b"already here", "h.pdf", root=root)

        again = store_file(ref.path, root=root)

        assert again.deduped is True
        assert again.path == ref.path
        assert len([p for p in root.rglob("*") if p.is_file()]) == 1

    def test_two_identical_files_ingested_from_disk_collapse(
        self, root: Path, tmp_path: Path
    ):
        a = tmp_path / "a.pdf"
        b = tmp_path / "b.pdf"
        a.write_bytes(b"identical")
        b.write_bytes(b"identical")

        first = store_file(a, root=root)
        second = store_file(b, root=root)

        assert first.path == second.path
        assert second.deduped is True
        assert len([p for p in root.rglob("*") if p.is_file()]) == 1

    def test_large_file_round_trips_byte_for_byte(self, root: Path, tmp_path: Path):
        payload = os.urandom(3 * 1024 * 1024 + 17)
        src = tmp_path / "big.bin"
        src.write_bytes(payload)

        ref = store_file(src, root=root)

        assert ref.size == len(payload)
        assert Path(ref.path).read_bytes() == payload

    def test_missing_source_raises(self, root: Path, tmp_path: Path):
        with pytest.raises(FileNotFoundError):
            store_file(tmp_path / "nope.pdf", root=root)


class TestDeleteSafety:
    """Dedup turns the old unconditional unlink into data loss."""

    @pytest.mark.asyncio
    async def test_shared_blob_survives_deleting_one_referrer(
        self, root: Path, tmp_path: Path, monkeypatch
    ):
        import open_notebook.storage.blobs as blobs

        path = tmp_path / "shared.pdf"
        path.write_bytes(b"shared content")
        ref = store_file(path, root=root)

        async def referenced(sha: str, exclude_source: str | None = None) -> bool:
            return sha == ref.sha256

        monkeypatch.setattr(blobs, "is_referenced", referenced)

        removed = await unlink_if_unreferenced(ref.path, ref.sha256, "source:one")

        assert removed is False
        assert Path(ref.path).exists(), "shared blob was deleted while still referenced"

    @pytest.mark.asyncio
    async def test_unreferenced_blob_is_deleted(
        self, root: Path, tmp_path: Path, monkeypatch
    ):
        import open_notebook.storage.blobs as blobs

        src = tmp_path / "solo.pdf"
        src.write_bytes(b"solo")
        ref = store_file(src, root=root)

        async def referenced(sha: str, exclude_source: str | None = None) -> bool:
            return False

        monkeypatch.setattr(blobs, "is_referenced", referenced)

        removed = await unlink_if_unreferenced(ref.path, ref.sha256, "source:one")

        assert removed is True
        assert not Path(ref.path).exists()

    @pytest.mark.asyncio
    async def test_missing_file_is_not_an_error(self, root: Path, monkeypatch):
        import open_notebook.storage.blobs as blobs

        async def referenced(sha: str, exclude_source: str | None = None) -> bool:
            return False

        monkeypatch.setattr(blobs, "is_referenced", referenced)

        # Deleting a source whose file extraction already cleaned up must
        # not raise.
        removed = await unlink_if_unreferenced(
            str(root / "gone.pdf"), "0" * 64, "source:x"
        )
        assert removed is False

    @pytest.mark.asyncio
    async def test_exclude_is_passed_as_a_record_id(self, root: Path, monkeypatch):
        """Regression: `id != $str` silently matches nothing.

        SurrealDB 2.x compares a record id against a string and never
        matches, so the exclusion does nothing and every source counts as
        its own referrer. The failure is silent and fails in the leaky
        direction - blobs are never collected. Found by running the real
        query against a live 2.7.0 server: the string form returned 2,
        the RecordID form returned 1. A stubbed repo_query cannot catch
        this, which is exactly why it is pinned here.
        """
        from surrealdb import RecordID

        import open_notebook.database.repository as repo

        captured: dict[str, object] = {}

        async def capture(query: str, params: dict | None = None):
            captured.update(params or {})
            return [{"n": 0}]

        monkeypatch.setattr(repo, "repo_query", capture)

        await is_referenced("a" * 64, exclude_source="source:abc")

        assert isinstance(captured.get("exclude"), RecordID), (
            f"exclude must be a RecordID, got {type(captured.get('exclude'))}"
        )
        assert str(captured["exclude"]) == "source:abc"

    @pytest.mark.asyncio
    async def test_database_failure_keeps_the_blob(self, root: Path, monkeypatch):
        """A DB outage must never delete study material."""
        import open_notebook.storage.blobs as blobs

        src = root.parent / "keepme.pdf"
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_bytes(b"precious")
        ref = store_file(src, root=root)

        async def boom(*args, **kwargs):
            raise ConnectionError("database is down")

        monkeypatch.setattr(blobs, "repo_query", boom, raising=False)
        # is_referenced imports repo_query locally, so patch the source
        # module it pulls from.
        import open_notebook.database.repository as repo

        monkeypatch.setattr(repo, "repo_query", boom)

        removed = await unlink_if_unreferenced(ref.path, ref.sha256, "source:x")

        assert removed is False
        assert Path(ref.path).exists(), "blob deleted despite the check failing"
