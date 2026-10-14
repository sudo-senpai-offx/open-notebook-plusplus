"""Deletions must survive the round trip.

The bug this exists to prevent is not hypothetical. A source deleted on
the phone was recreated by the next export from the PC, because the PC
had never heard of the deletion - absence is not information, and git
cannot tell "never exported" from "deleted".

These tests pin the two halves separately because they fail differently.
The unit tests below need no database and cover the sweep, the
timestamp rule and the storage format. The round trip needs two live
namespaces standing in for two devices, because the interesting failure
was only ever visible against a real one.

    SURREAL_TEST_URL=ws://127.0.0.1:8108/rpc \\
    SURREAL_TEST_USER=root SURREAL_TEST_PASSWORD=root \\
        pytest tests/test_sync_tombstones.py
"""

import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from open_notebook.sync.tombstones import (
    TOMBSTONES_DIR,
    load_tombstones,
    prune_tombstones,
    sweep,
    sweep_notebooks,
    write_tombstone,
)

STAMP = "2026-10-14T12:00:00+00:00"


def _seed(root: Path, name: str, record_id: str, **fields) -> None:
    (root / "sources").mkdir(parents=True, exist_ok=True)
    payload = {"id": record_id, "updated": STAMP, **fields}
    (root / "sources" / f"{record_id.replace(':', '-')}.json").write_text(
        json.dumps(payload)
    )


class TestSweep:
    def test_a_vanished_record_is_tombstoned(self, tmp_path):
        _seed(tmp_path, "sources", "source:gone", sha256="abc123")

        written = sweep(tmp_path, {"sources": ["source:alive"]})

        assert [t.id for t in written] == ["source:gone"]
        assert written[0].sha256 == "abc123"

    def test_a_live_record_is_left_alone(self, tmp_path):
        _seed(tmp_path, "sources", "source:alive")

        written = sweep(tmp_path, {"sources": ["source:alive"]})

        assert written == []
        assert not (tmp_path / TOMBSTONES_DIR).exists()
        assert (tmp_path / "sources" / "source-alive.json").exists()

    def test_the_stale_manifest_file_is_removed(self, tmp_path):
        """Otherwise every future import deletes the record again."""
        _seed(tmp_path, "sources", "source:gone")

        sweep(tmp_path, {"sources": []})

        assert not (tmp_path / "sources" / "source-gone.json").exists()
        assert load_tombstones(tmp_path)

    def test_a_corrupt_manifest_file_is_not_a_deletion(self, tmp_path):
        """Truncated file is not evidence of anything."""
        (tmp_path / "sources").mkdir(parents=True)
        (tmp_path / "sources" / "source-broken.json").write_text("{not json")

        assert sweep(tmp_path, {"sources": []}) == []

    def test_a_missing_directory_is_not_an_error(self, tmp_path):
        assert sweep(tmp_path, {"sources": ["source:x"]}) == []

    def test_every_directory_is_swept(self, tmp_path):
        _seed(tmp_path, "sources", "source:s")
        (tmp_path / "notes").mkdir(parents=True, exist_ok=True)
        (tmp_path / "notes" / "note-n.json").write_text(
            json.dumps({"id": "note:n", "updated": STAMP})
        )

        written = sweep(tmp_path, {"sources": [], "notes": []})

        assert {t.id for t in written} == {"source:s", "note:n"}


class TestTimestamp:
    def test_a_tombstone_outranks_the_record_it_deletes(self):
        """The clock-skew case.

        Two devices, two clocks. If the phone runs an hour behind and
        deletes something the PC touched a minute ago, a naive `now()`
        loses the deletion to the record it is deleting - and the user
        watches their own delete bounce back.
        """
        recent = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()

        tombstone = write_tombstone(
            Path("/tmp/does-not-matter"), "source:x", "sources", recent
        )

        deleted_at = tombstone.deleted_at
        assert deleted_at is not None
        assert deleted_at > datetime.fromisoformat(recent)

    def test_a_stale_record_does_not_delay_the_tombstone(self):
        """A tombstone for a month-old record is stamped now, not then."""
        old = "2020-01-01T00:00:00+00:00"

        tombstone = write_tombstone(
            Path("/tmp/does-not-matter"), "source:y", "sources", old
        )

        deleted_at = tombstone.deleted_at
        assert deleted_at is not None
        assert deleted_at > datetime.fromisoformat(old)
        assert deleted_at.year >= 2026

    def test_a_naive_timestamp_is_read_as_utc(self, tmp_path):
        """Two devices are not guaranteed to agree on a timezone.

        Reading a naive stamp as local time would shift a deletion by the
        receiver's offset, which is a silent and very confusing bug.
        """
        (tmp_path / TOMBSTONES_DIR).mkdir(parents=True)
        (tmp_path / TOMBSTONES_DIR / "source-n.json").write_text(
            json.dumps(
                {
                    "id": "source:n",
                    "kind": "sources",
                    "updated": "2026-10-14T12:00:00",
                    "deleted": True,
                }
            )
        )

        parsed = load_tombstones(tmp_path)["source:n"].deleted_at

        assert parsed is not None
        assert parsed.tzinfo is timezone.utc


class TestLoading:
    def test_the_newest_tombstone_wins(self, tmp_path):
        """A record can be deleted, edited back into life, deleted again."""
        for stamp in ("2026-01-01T00:00:00+00:00", "2026-06-01T00:00:00+00:00"):
            (tmp_path / TOMBSTONES_DIR).mkdir(parents=True, exist_ok=True)
            (tmp_path / TOMBSTONES_DIR / "source-r.json").write_text(
                json.dumps(
                    {
                        "id": "source:r",
                        "kind": "sources",
                        "updated": stamp,
                        "deleted": True,
                    }
                )
            )

        found = load_tombstones(tmp_path)

        assert len(found) == 1
        deleted_at = found["source:r"].deleted_at
        assert deleted_at is not None and deleted_at.month == 6

    def test_a_record_without_deleted_is_not_a_tombstone(self, tmp_path):
        (tmp_path / TOMBSTONES_DIR).mkdir(parents=True)
        (tmp_path / TOMBSTONES_DIR / "source-u.json").write_text(
            json.dumps({"id": "source:u", "kind": "sources", "updated": STAMP})
        )

        assert load_tombstones(tmp_path) == {}

    def test_an_unreadable_file_is_skipped_not_fatal(self, tmp_path):
        (tmp_path / TOMBSTONES_DIR).mkdir(parents=True)
        (tmp_path / TOMBSTONES_DIR / "broken.json").write_text("{oh dear")

        assert load_tombstones(tmp_path) == {}


class TestPruning:
    def test_only_old_tombstones_go(self, tmp_path):
        for record_id, stamp in (
            ("source:old", "2020-01-01T00:00:00+00:00"),
            ("source:new", "2999-01-01T00:00:00+00:00"),
        ):
            (tmp_path / TOMBSTONES_DIR).mkdir(parents=True, exist_ok=True)
            (
                tmp_path / TOMBSTONES_DIR / f"{record_id.replace(':', '-')}.json"
            ).write_text(
                json.dumps(
                    {
                        "id": record_id,
                        "kind": "sources",
                        "updated": stamp,
                        "deleted": True,
                    }
                )
            )

        removed = prune_tombstones(tmp_path, older_than_days=30)

        assert removed == ["source:old"]
        assert "source:new" in load_tombstones(tmp_path)


class TestNotebookSweep:
    """Notebooks share one index file, so the sweep has different rules.

    The ordering is load-bearing and was got wrong once: writing the
    index before sweeping erased the only evidence of what the peer
    believed existed, and every deletion silently stopped propagating.
    """

    def _index(self, root: Path, entries: list[tuple[str, str]]) -> None:
        (root / "notebooks").mkdir(parents=True, exist_ok=True)
        (root / "notebooks" / "index.json").write_text(
            json.dumps(
                {
                    "notebooks": [
                        {"slug": slug, "name": name, "updated": STAMP}
                        for slug, name in entries
                    ]
                }
            )
        )

    def test_a_vanished_notebook_is_tombstoned_by_slug(self, tmp_path):
        from open_notebook.sync.tombstones import NOTEBOOKS_KIND, notebook_id

        self._index(
            tmp_path, [("genetics-301", "Genetics 301"), ("dropped", "Dropped")]
        )

        written = sweep_notebooks(tmp_path, "notebooks/index.json", ["genetics-301"])

        assert len(written) == 1
        assert written[0].id == notebook_id("dropped")
        assert written[0].kind == NOTEBOOKS_KIND
        assert notebook_id("dropped") == "notebook:dropped"

    def test_the_dead_entry_is_dropped_from_the_index(self, tmp_path):
        self._index(tmp_path, [("kept", "Kept"), ("dropped", "Dropped")])

        sweep_notebooks(tmp_path, "notebooks/index.json", ["kept"])

        remaining = json.loads((tmp_path / "notebooks" / "index.json").read_text())[
            "notebooks"
        ]
        assert [nb["slug"] for nb in remaining] == ["kept"]

    def test_a_live_notebook_is_untouched(self, tmp_path):
        self._index(tmp_path, [("kept", "Kept")])

        assert sweep_notebooks(tmp_path, "notebooks/index.json", ["kept"]) == []
        assert not (tmp_path / TOMBSTONES_DIR).exists()

    def test_a_missing_index_is_not_an_error(self, tmp_path):
        assert sweep_notebooks(tmp_path, "notebooks/index.json", ["x"]) == []

    def test_a_corrupt_index_is_not_a_mass_deletion(self, tmp_path):
        """A truncated index must not tombstone every notebook in it."""
        (tmp_path / "notebooks").mkdir(parents=True, exist_ok=True)
        (tmp_path / "notebooks" / "index.json").write_text("{truncated")

        assert sweep_notebooks(tmp_path, "notebooks/index.json", []) == []


# --- live round trip -------------------------------------------------------
#
# Two namespaces on one server stand in for two devices. One namespace
# would let the import see its own export and skip everything as stale,
# passing while testing nothing. The fixture mirrors
# test_sync_sources_roundtrip.py because the repository reads
# configuration straight from the environment on every call - swapping
# SURREAL_NAMESPACE alone is not enough, the database name moves too.

# Scoped to the live tests rather than set as a module-level `pytestmark`,
# which would skip the unit tests above too - and a skipped suite that
# looks green is worse than a red one.
requires_db = pytest.mark.skipif(
    not os.getenv("SURREAL_TEST_URL"),
    reason="needs a live SurrealDB; set SURREAL_TEST_URL",
)


@pytest.fixture
def use_device(monkeypatch):
    """Point the app at a throwaway namespace per simulated device.

    Namespaces are memoised per label. Minting a fresh one on every call
    makes "switch back to the PC" land in an empty database, and the test
    then passes or fails for reasons that have nothing to do with sync.
    """
    issued = []
    by_label = {}

    def use(label: str) -> str:
        if label not in by_label:
            ns = f"tombtest_{label}_{uuid.uuid4().hex[:8]}"
            by_label[label] = ns
            issued.append(ns)
        ns = by_label[label]
        monkeypatch.setenv("SURREAL_NAMESPACE", ns)
        monkeypatch.setenv("SURREAL_DATABASE", ns)
        return ns

    monkeypatch.setenv("SURREAL_URL", os.environ["SURREAL_TEST_URL"])
    monkeypatch.setenv("SURREAL_USER", os.getenv("SURREAL_TEST_USER", "root"))
    monkeypatch.setenv("SURREAL_PASSWORD", os.getenv("SURREAL_TEST_PASSWORD", "root"))
    use("a")

    yield use

    import asyncio

    from open_notebook.database.repository import repo_query

    async def wipe_all() -> None:
        for ns in issued:
            monkeypatch.setenv("SURREAL_NAMESPACE", ns)
            monkeypatch.setenv("SURREAL_DATABASE", ns)
            for table in ("source", "note", "flashcard", "artifact", "notebook"):
                try:
                    await repo_query(f"DELETE {table}")
                except Exception:
                    pass

    try:
        asyncio.run(wipe_all())
    except Exception:
        pass


@requires_db
@pytest.mark.asyncio
async def test_a_deletion_survives_the_round_trip(tmp_path, use_device):
    """The whole point, end to end.

    The PC holds a source, the phone receives it over the wire, the
    phone deletes it, and the PC imports. The PC must end up without it.
    Before tombstones this silently un-happened: the PC's copy was
    exported and the deletion reverted.
    """
    from open_notebook.domain.notebook import Source
    from open_notebook.sync.exchange import export_all, import_all

    use_device("pc")
    source = Source(title="Doomed lecture", topics=[])
    await source.save()
    record_id = str(source.id)

    # Export while still pointed at the PC: export_all reads the current
    # namespace, so switching first would ship an empty manifest.
    await export_all(device="pc", root=tmp_path / "pc")

    use_device("phone")
    result = await import_all(root=tmp_path / "pc")
    assert result["sources_created"] == 1
    assert await _exists(Source, record_id), "precondition: the phone has it"

    await (await Source.get(record_id)).delete()
    assert not await _exists(Source, record_id)
    await export_all(device="phone", root=tmp_path / "pc")

    use_device("pc")
    result = await import_all(root=tmp_path / "pc")

    assert result["tombstones_applied"] == 1
    assert not await _exists(Source, record_id), (
        "the deletion did not survive the round trip - this is the exact "
        "failure the tombstones exist to prevent"
    )


@requires_db
@pytest.mark.asyncio
async def test_a_local_edit_overrides_a_remote_deletion(tmp_path, use_device):
    """Last-write-wins, including edits.

    Delete on the phone, then edit on the PC. The edit is the newer fact,
    so the record comes back. This is the rule that was chosen: the most
    recent activity wins, whichever device it happened on, so a delete is
    a write like any other rather than a permanent verdict.
    """
    from open_notebook.domain.notebook import Source
    from open_notebook.sync.exchange import export_all, import_all

    use_device("pc")
    source = Source(title="Contested", topics=[])
    await source.save()
    record_id = str(source.id)

    await export_all(device="pc", root=tmp_path / "pc")

    use_device("phone")
    await import_all(root=tmp_path / "pc")
    await (await Source.get(record_id)).delete()

    # Written as a file rather than through `write_tombstone`, which
    # deliberately stamps every tombstone *after* the record it deletes
    # so that clock skew cannot lose a local delete. Here the tombstone
    # must be the older of the two facts, which is the situation the
    # real clock would produce when the delete genuinely came first.
    # Unambiguously older than any save made during this test, so the
    # assertion tests the comparison rather than the wall clock.
    ancient = "2020-01-01T00:00:00+00:00"
    tombstone_dir = tmp_path / "pc" / TOMBSTONES_DIR
    tombstone_dir.mkdir(parents=True, exist_ok=True)
    (tombstone_dir / f"{record_id.replace(':', '-')}.json").write_text(
        json.dumps(
            {
                "id": record_id,
                "kind": "sources",
                "updated": ancient,
                "deleted": True,
            }
        )
    )

    use_device("pc")
    local = await Source.get(record_id)
    local.title = "Contested, edited on the PC"
    await local.save()

    result = await import_all(root=tmp_path / "pc")

    assert result["tombstones_overridden_by_local_edit"] == 1
    assert result["tombstones_applied"] == 0
    assert await _exists(Source, record_id), (
        "a newer local edit must beat an older tombstone"
    )


@requires_db
@pytest.mark.asyncio
async def test_deleting_a_source_keeps_the_bytes(tmp_path, use_device):
    """Forget, not destroy.

    The record goes; the file stays. If the device applying a tombstone
    were the last one holding the bytes, unlinking would turn someone
    else's delete into permanent data loss.
    """
    from open_notebook.domain.notebook import Asset, Source
    from open_notebook.storage.blobs import store_file

    use_device("pc")
    document = tmp_path / "lecture.pdf"
    document.write_bytes(b"%PDF-1.4 fake lecture bytes")
    stored = store_file(document, root=tmp_path / "blobs")
    assert Path(stored.path).exists()

    source = Source(
        title="Kept",
        asset=Asset(
            file_path=stored.path, sha256=stored.sha256, filename="lecture.pdf"
        ),
    )
    await source.save()

    await source.delete()

    assert not await _exists(Source, str(source.id)), "the record must be gone"
    assert Path(stored.path).exists(), (
        "the blob must survive: delete means forget, not destroy"
    )


async def _exists(model, record_id: str) -> bool:
    try:
        await model.get(record_id)
        return True
    except Exception:
        return False


@requires_db
@pytest.mark.asyncio
async def test_a_notebook_reaches_the_other_device(tmp_path, use_device):
    """Notebooks sync at all now.

    The index was written by every export and read by nothing, so a
    notebook that existed only on the PC never appeared on the phone.
    This is why seven course notebooks were missing from the device.
    """
    from open_notebook.domain.notebook import Notebook
    from open_notebook.sync.exchange import export_all, import_all

    use_device("pc")
    for name in ("Genetics 301", "日本語勉強会", "Molecular Biology"):
        await Notebook(name=name, description="").save()

    await export_all(device="pc", root=tmp_path / "pc")

    use_device("phone")
    result = await import_all(root=tmp_path / "pc")

    assert result["notebooks_created"] == 3
    assert result["missing_notebooks"] == []

    names = {r["name"] for r in await _query("SELECT name FROM notebook")}
    assert names == {"Genetics 301", "日本語勉強会", "Molecular Biology"}


@requires_db
@pytest.mark.asyncio
async def test_a_deleted_notebook_stays_deleted(tmp_path, use_device):
    """The gap that was flagged rather than papered over."""
    from open_notebook.domain.notebook import Notebook
    from open_notebook.sync.exchange import export_all, import_all

    use_device("pc")
    await Notebook(name="Doomed course", description="").save()
    await export_all(device="pc", root=tmp_path / "pc")

    use_device("phone")
    await import_all(root=tmp_path / "pc")
    assert await _notebook_exists("Doomed course"), "precondition: phone has it"

    await (await _get_notebook("Doomed course")).delete()
    await export_all(device="phone", root=tmp_path / "pc")

    use_device("pc")
    result = await import_all(root=tmp_path / "pc")

    assert result["tombstones_applied"] == 1
    assert not await _notebook_exists("Doomed course"), (
        "a deleted notebook resurrected - the gap this closes"
    )


async def _query(statement: str):
    from open_notebook.database.repository import repo_query

    return await repo_query(statement)


async def _get_notebook(name: str):
    from open_notebook.domain.notebook import Notebook

    rows = await _query(f"SELECT * FROM notebook WHERE name = '{name}'")
    return await Notebook.get(str(rows[0]["id"]))


async def _notebook_exists(name: str) -> bool:
    rows = await _query(f"SELECT * FROM notebook WHERE name = '{name}'")
    return bool(rows)
