"""A conversation crosses between devices without ever entering git.

The invariant under test first: the sync repository holds metadata and
hashes only. A transcript is course content - the same material as a
lecture PDF - so the moment it lands in a git repo, the repo stops being
metadata and becomes a library. Bytes go over the blob channel keyed by
hash; the manifest records only the hash.

The rest of the file tests what makes the union safe: idempotence,
append-only merging, and the difference between "the manifest knows
about bytes I have not fetched" and "this failed".
"""

import json
import os
import shutil
import uuid
from pathlib import Path

import pytest

from open_notebook.sync import chatsync

pytestmark = pytest.mark.skipif(
    not os.getenv("SURREAL_TEST_URL"),
    reason="needs a live SurrealDB; set SURREAL_TEST_URL",
)

# Distinctive enough that finding it anywhere in the sync tree is
# unambiguous, and unlike a real answer it cannot turn up by accident in
# a notebook name, a title or a timestamp.
MARKER = "ZEBRAFISH-CANARY-8f21a4"


@pytest.fixture
def two_devices(monkeypatch, tmp_path):
    """Two throwaway devices, each with its own namespace *and* blob store.

    Both have to switch together. A shared blob root would let the round
    trip pass for the wrong reason - device B would simply read what
    device A had written, and the bytes would never actually have been
    transferred. That is the one failure this file exists to catch.
    """
    import open_notebook.config as config

    issued: list[str] = []
    roots = {"a": tmp_path / "blobs_a", "b": tmp_path / "blobs_b"}
    for path in roots.values():
        path.mkdir(parents=True, exist_ok=True)

    def use(label: str) -> str:
        ns = f"chatsync_{label}_{uuid.uuid4().hex[:8]}"
        issued.append(ns)
        monkeypatch.setenv("SURREAL_NAMESPACE", ns)
        monkeypatch.setenv("SURREAL_DATABASE", ns)
        # blobs._root() re-reads this attribute on each call, so patching
        # it is enough to redirect the whole store. An env var would not
        # be: config.py computes BLOBS_FOLDER once at import.
        monkeypatch.setattr(config, "BLOBS_FOLDER", str(roots[label]), raising=False)
        # Same for local sync state: the baseline lives under
        # DATA_FOLDER, read at call time, so each device remembers only
        # what it has seen.
        monkeypatch.setenv("DATA_FOLDER", str(tmp_path / f"data_{label}"))
        return ns

    monkeypatch.setenv("SURREAL_URL", os.environ["SURREAL_TEST_URL"])
    monkeypatch.setenv("SURREAL_USER", os.getenv("SURREAL_TEST_USER", "root"))
    monkeypatch.setenv("SURREAL_PASSWORD", os.getenv("SURREAL_TEST_PASSWORD", "root"))
    use("a")

    yield use

    import asyncio

    from open_notebook.database.repository import repo_query

    async def wipe() -> None:
        for ns in issued:
            monkeypatch.setenv("SURREAL_NAMESPACE", ns)
            monkeypatch.setenv("SURREAL_DATABASE", ns)
            for table in ("chat_message", "chat_session"):
                try:
                    await repo_query(f"DELETE {table}")  # noqa: S608
                except Exception:
                    pass

    try:
        asyncio.run(wipe())
    except Exception:
        pass


class _Turn:
    """A message shaped the way the checkpoint hands one over."""

    def __init__(self, role: str, content: str, msg_id: str) -> None:
        self.type = role
        self.content = content
        self.id = msg_id


async def _session(title: str, messages: list[tuple[str, str]]) -> str:
    """Create a session with messages. Returns its record id."""
    from open_notebook.database.repository import ensure_record_id, repo_query
    from open_notebook.domain.notebook import persist_chat_messages

    sid = f"chat_session:{uuid.uuid4().hex[:12]}"
    await repo_query(
        "CREATE $id CONTENT { title: $title, updated: time::now() }",
        {"id": ensure_record_id(sid), "title": title},
    )
    await persist_chat_messages(
        sid,
        [_Turn(role, content, f"m{i}") for i, (role, content) in enumerate(messages)],
    )
    return sid


async def _transcript_of(session_id: str) -> list[dict]:
    from open_notebook.database.repository import ensure_record_id, repo_query

    rows = await repo_query(
        "SELECT role, content, seq FROM chat_message WHERE session = $sid ORDER BY seq ASC",
        {"sid": ensure_record_id(session_id)},
    )
    return [dict(r) for r in (rows or [])]


async def _only_session() -> str:
    from open_notebook.domain.notebook import ChatSession

    sessions = await ChatSession.get_all()
    assert len(sessions) == 1, f"expected one session, found {len(sessions)}"
    return str(sessions[0].id)


def _send_blobs(source: Path, target: Path) -> int:
    """Stand in for the blob channel: move one device's bytes to another.

    Deliberately a file copy rather than a call into blobtransfer: this
    tests that the manifest and the store agree on a hash, which is the
    part that can silently break. The HTTP transfer itself is covered
    where it lives.
    """
    moved = 0
    for shard in source.iterdir():
        if not shard.is_dir():
            continue
        destination = target / shard.name
        destination.mkdir(parents=True, exist_ok=True)
        for entry in shard.iterdir():
            shutil.copy(entry, destination / entry.name)
            moved += 1
    return moved


class TestNothingCourseShapedEntersGit:
    """The rule the whole design exists to keep."""

    @pytest.mark.asyncio
    async def test_the_sync_tree_contains_no_transcript_text(
        self, two_devices, tmp_path
    ):
        root = tmp_path / "sync"

        from open_notebook.sync.exchange import export_all

        await _session("Genomics primer", [("human", f"How do I design one? {MARKER}")])
        await export_all("pc", root=root)

        offenders = [
            str(p.relative_to(root))
            for p in root.rglob("*")
            if p.is_file() and MARKER.encode() in p.read_bytes()
        ]

        assert offenders == [], f"transcript text reached the sync repo via {offenders}"

    @pytest.mark.asyncio
    async def test_the_manifest_carries_the_hash_and_not_the_words(
        self, two_devices, tmp_path
    ):
        root = tmp_path / "sync"

        from open_notebook.sync.exchange import export_all

        sid = await _session("PCR", [("human", f"PCR troubleshooting {MARKER}")])
        manifest = await export_all("pc", root=root)

        entry = json.loads(
            (root / "chat" / f"{sid.replace(':', '-')}.json").read_text()
        )

        assert entry["transcript_sha256"], "the hash is what makes the bytes findable"
        assert entry["message_count"] == 1
        assert manifest["counts"]["chats"] == 1
        assert MARKER not in json.dumps(entry)


class TestRoundTrip:
    @pytest.mark.asyncio
    async def test_a_conversation_arrives_intact(self, two_devices, tmp_path):
        use = two_devices
        root = tmp_path / "sync"

        from open_notebook.sync.exchange import export_all, import_all

        use("a")
        await _session(
            "Titration",
            [("human", "What is the first step?"), ("ai", f"Mix the buffer. {MARKER}")],
        )
        await export_all("pc", root=root)

        roots = _blob_roots(tmp_path)
        _send_blobs(roots["a"], roots["b"])

        use("b")
        result = await import_all(root=root, dry_run=False)
        arrived = await _transcript_of(await _only_session())

        assert result["chats_created"] == 1
        assert result["chats_messages_added"] == 2
        assert [m["role"] for m in arrived] == ["human", "ai"]
        assert arrived[1]["content"].endswith(MARKER)

    @pytest.mark.asyncio
    async def test_importing_twice_adds_nothing_the_second_time(
        self, two_devices, tmp_path
    ):
        use = two_devices
        root = tmp_path / "sync"

        from open_notebook.sync.exchange import export_all, import_all

        use("a")
        await _session("Buffers", [("human", "pH?"), ("ai", "7.0")])
        await export_all("pc", root=root)

        roots = _blob_roots(tmp_path)
        _send_blobs(roots["a"], roots["b"])

        use("b")
        first = await import_all(root=root, dry_run=False)
        second = await import_all(root=root, dry_run=False)

        assert first["chats_messages_added"] == 2
        assert second["chats_messages_added"] == 0, "a repeated sync must not duplicate"

    @pytest.mark.asyncio
    async def test_two_devices_can_both_append_to_one_conversation(
        self, two_devices, tmp_path
    ):
        """What makes this a union rather than a last-write-wins overwrite."""
        use = two_devices
        root = tmp_path / "sync"

        from open_notebook.domain.notebook import persist_chat_messages
        from open_notebook.sync.exchange import export_all, import_all

        use("a")
        sid = await _session("Shared", [("human", "from the PC")])
        await export_all("pc", root=root)

        roots = _blob_roots(tmp_path)
        _send_blobs(roots["a"], roots["b"])

        # The phone types into the same conversation, then the PC pulls.
        use("b")
        await import_all(root=root, dry_run=False)
        await persist_chat_messages(sid, [_Turn("human", "from the phone", "pz1")])
        await export_all("phone", root=root)
        _send_blobs(roots["b"], roots["a"])

        use("a")
        await import_all(root=root, dry_run=False)

        contents = {m["content"] for m in await _transcript_of(sid)}
        assert contents == {"from the PC", "from the phone"}


class TestPendingIsNotFailure:
    @pytest.mark.asyncio
    async def test_a_session_imports_before_its_bytes_arrive(
        self, two_devices, tmp_path
    ):
        use = two_devices
        root = tmp_path / "sync"

        from open_notebook.sync.exchange import export_all, import_all

        use("a")
        await _session("Sequencing", [("human", "depth?"), ("ai", "30x")])
        await export_all("pc", root=root)

        # Note: no _send_blobs. The manifest names a transcript this
        # device has never seen.
        use("b")
        result = await import_all(root=root, dry_run=False)

        assert result["chats_created"] == 1, "the session exists and is usable"
        assert result["chats_awaiting_blobs"] == 1
        assert result["chats_messages_added"] == 0

    @pytest.mark.asyncio
    async def test_re_exporting_keeps_the_hash_it_cannot_yet_serve(
        self, two_devices, tmp_path
    ):
        """Pull, import, export: the hash must survive the round trip.

        A device that imported a session but not its transcript has no
        messages to serialise. Writing `transcript_sha256: null` on the
        way out would drop the only record that bytes exist, so the peer
        would never push them and the conversation would stay
        half-synced permanently - with no error anywhere. This is the
        planned sync order, not a corner.
        """
        use = two_devices
        root = tmp_path / "sync"

        from open_notebook.sync.exchange import export_all, import_all

        use("a")
        sid = await _session("Half here", [("human", "q"), ("ai", "a")])
        await export_all("pc", root=root)
        published = json.loads(
            (root / "chat" / f"{sid.replace(':', '-')}.json").read_text()
        )

        # The phone pulls the metadata, cannot fetch the bytes, and
        # then exports its own copy back.
        use("b")
        await import_all(root=root, dry_run=False)
        await export_all("phone", root=root)

        republished = json.loads(
            (root / "chat" / f"{sid.replace(':', '-')}.json").read_text()
        )

        assert republished["transcript_sha256"] == published["transcript_sha256"]
        assert "transcript_exported_by" not in republished, (
            "it must not claim bytes it does not hold - that claim is what "
            "tells the peer to push"
        )

    @pytest.mark.asyncio
    async def test_the_hash_survives_even_when_the_peer_re_exports(
        self, two_devices, tmp_path
    ):
        """And the count that drives the push reflects it."""
        use = two_devices
        root = tmp_path / "sync"

        from open_notebook.sync.exchange import export_all, import_all

        use("a")
        await _session("Pending", [("human", "q")])
        await export_all("pc", root=root)

        use("b")
        await import_all(root=root, dry_run=False)
        manifest = await export_all("phone", root=root)

        assert manifest["chats_awaiting_blobs"] == 1, (
            "one session names a transcript this device does not hold, and "
            "that is exactly what the blob channel needs to be told"
        )

    @pytest.mark.asyncio
    async def test_the_bytes_are_picked_up_once_they_land(self, two_devices, tmp_path):
        use = two_devices
        root = tmp_path / "sync"

        from open_notebook.sync.exchange import export_all, import_all

        use("a")
        sid = await _session(
            "Libraries", [("human", "which kit?"), ("ai", "the second")]
        )
        await export_all("pc", root=root)

        use("b")
        await import_all(root=root, dry_run=False)
        assert await _transcript_of(sid) == []

        _send_blobs(*_ordered_roots(tmp_path))

        result = await import_all(root=root, dry_run=False)
        assert result["chats_messages_added"] == 2
        assert len(await _transcript_of(sid)) == 2


def _blob_roots(tmp_path) -> dict:
    return {"a": tmp_path / "blobs_a", "b": tmp_path / "blobs_b"}


def _ordered_roots(tmp_path) -> tuple[Path, Path]:
    roots = _blob_roots(tmp_path)
    return roots["a"], roots["b"]


class TestTheBlob:
    def test_the_same_conversation_hashes_the_same_way(self):
        """Or two devices push two blobs for one conversation, forever."""

        class M:
            def __init__(self, **kw):
                self.__dict__.update(kw)

        rows = [
            M(
                id="chat_message:1",
                seq=0,
                role="human",
                content="q",
                created="2026-10-14T00:00:00+00:00",
            ),
            M(
                id="chat_message:2",
                seq=1,
                role="ai",
                content="a",
                created="2026-10-14T00:00:01+00:00",
            ),
        ]

        # Order of arrival differs from order of conversation; the hash
        # must not depend on it.
        assert chatsync.serialize_transcript(
            "chat_session:x", rows
        ) == chatsync.serialize_transcript("chat_session:x", list(reversed(rows)))

    def test_different_conversations_hash_differently(self):
        class M:
            def __init__(self, **kw):
                self.__dict__.update(kw)

        one = [M(id="chat_message:1", seq=0, role="human", content="q")]
        two = [M(id="chat_message:1", seq=0, role="human", content="different")]

        assert chatsync.serialize_transcript("s", one) != chatsync.serialize_transcript(
            "s", two
        )

    def test_dict_rows_keep_their_content(self):
        """A real bug, caught by a test that expected a size warning.

        `serialize_transcript` read attributes only. `parse_transcript`
        returns dicts, so re-serialising a parsed transcript turned every
        message into an empty one - a conversation reduced to a fraction
        of its size, hashed consistently, and valid at every step along
        the way. Nothing raised.

        What surfaced it was the size advisory *not* appearing, so the
        assertion that noticed was an unrelated-looking one.
        """
        rows = [
            {"id": "chat_message:1", "seq": 0, "role": "human", "content": "q"},
            {"id": "chat_message:2", "seq": 1, "role": "ai", "content": "a" * 5000},
        ]

        payload = chatsync.parse_transcript(chatsync.serialize_transcript("s", rows))

        assert [m["content"] for m in payload["messages"]] == ["q", "a" * 5000]

    def test_object_rows_and_dict_rows_serialise_identically(self):
        """The two forms are interchangeable, not merely both tolerated."""

        class M:
            def __init__(self, **kw):
                self.__dict__.update(kw)

        as_objects = [
            M(id="chat_message:1", seq=0, role="human", content="q", created=None),
            M(id="chat_message:2", seq=1, role="ai", content="a", created=None),
        ]
        as_dicts = [
            {"id": "chat_message:1", "seq": 0, "role": "human", "content": "q"},
            {"id": "chat_message:2", "seq": 1, "role": "ai", "content": "a"},
        ]

        assert chatsync.serialize_transcript("s", as_objects) == (
            chatsync.serialize_transcript("s", as_dicts)
        )

    def test_an_unreadable_transcript_raises_rather_than_reading_empty(self):
        """An empty parse looks exactly like an empty conversation."""
        with pytest.raises(chatsync.TranscriptError):
            chatsync.parse_transcript(b"not json at all")

    def test_an_unknown_format_is_refused(self):
        with pytest.raises(chatsync.TranscriptError):
            chatsync.parse_transcript(b'{"format":"something-else/9","messages":[]}')

    def test_a_truncated_transcript_is_refused(self):
        """The failure mode of a transfer cut short halfway."""
        good = chatsync.serialize_transcript(
            "chat_session:x",
            [{"id": "chat_message:1", "seq": 0, "role": "human", "content": "x"}],
        )
        with pytest.raises(chatsync.TranscriptError):
            chatsync.parse_transcript(good[: len(good) // 2])

    def test_a_large_transcript_warns_but_is_still_stored(self):
        """A conversation too big to keep is not one to discard.

        loguru does not route through caplog, so this attaches its own
        sink. Asserting against caplog here would see nothing at all and
        pass for the wrong reason - the same shape as a detector that
        returns a plausible zero.
        """
        from loguru import logger

        captured: list[str] = []
        sink_id = logger.add(lambda msg: captured.append(str(msg)), level="WARNING")
        try:
            big = [
                {
                    "id": f"chat_message:{i}",
                    "seq": i,
                    "role": "human",
                    "content": "x" * 4096,
                }
                for i in range(100)
            ]
            ref = chatsync.store_transcript("chat_session:big", big)
        finally:
            logger.remove(sink_id)

        assert chatsync.blob_exists(ref.sha256), "oversize must not mean dropped"
        assert any("advisory" in m for m in captured), f"no advisory logged: {captured}"

    def test_roles_are_coerced_to_what_the_schema_accepts(self):
        """The column asserts membership, so a bad value fails the write."""
        assert chatsync._normalise_role("assistant") == "ai"
        assert chatsync._normalise_role("user") == "human"
        assert chatsync._normalise_role("nonsense") == "human"


class TestDeletion:
    @pytest.mark.asyncio
    async def test_a_deleted_session_is_tombstoned(self, two_devices, tmp_path):
        root = tmp_path / "sync"

        from open_notebook.database.repository import (
            repo_delete,
        )
        from open_notebook.sync import tombstones
        from open_notebook.sync.exchange import export_all

        await _session("Temporary", [("human", "hi")])

        # Record the session in the baseline, then delete it. The
        # baseline is what arms the sweep: a device tombstones only ids
        # it has demonstrably seen. Backdating is unnecessary - age alone
        # is not evidence either way, which is the lesson of the 7 lost
        # notebooks.
        await export_all("pc", root=root)
        assert list((root / "chat").glob("*.json"))

        sid = await _only_session()
        tombstones.write_baseline({"chat": [sid]})

        # repo_delete, not `DELETE $id`: SurrealDB 2.x rejects a DELETE
        # whose target is a plain record value, and the error arrives as
        # an InternalError rather than anything mentioning the syntax.

        await repo_delete(await _only_session())
        manifest = await export_all("pc", root=root)

        assert manifest["counts"]["tombstoned"] >= 1
        assert not list((root / "chat").glob("*.json"))
        assert tombstones.load_tombstones(root), "the peer must be told"

    @pytest.mark.asyncio
    async def test_applying_a_chat_tombstone_clears_the_transcript(
        self, two_devices, tmp_path
    ):
        """Orphans would be adopted by a session re-imported under one id."""
        use = two_devices
        use("b")
        root = tmp_path / "sync"
        root.mkdir(parents=True)

        from open_notebook.sync import tombstones
        from open_notebook.sync.exchange import _apply_tombstones

        sid = await _session("Doomed", [("human", "bye"), ("ai", "later")])
        assert len(await _transcript_of(sid)) == 2

        # previous_updated is set past the record's own timestamp, so the
        # tombstone is the newer fact and wins the last-write-wins.
        tombstones.write_tombstone(
            root, sid, "chat", previous_updated="2099-01-01T00:00:00+00:00"
        )
        result = {
            "tombstones_applied": 0,
            "tombstones_overridden_by_local_edit": 0,
            "tombstones_already_absent": 0,
            "errors": [],
        }
        await _apply_tombstones(root, result, dry_run=False)

        assert result["tombstones_applied"] == 1
        assert await _transcript_of(sid) == []


class TestTheKeepBlobTrap:
    """A bug found while adding sessions to the tombstone path.

    `_apply_tombstones` called `existing.delete(keep_blob=True)`
    unconditionally. Only `Source.delete` accepts that argument; Note,
    Flashcard and ChatSession inherit `ObjectModel.delete(self)`. So a
    tombstone for a record this device actually held raised TypeError -
    and no test caught it, because `keep_blob` appeared nowhere in them.
    Deleting a note on the phone would have killed the PC's import.
    """

    @pytest.mark.parametrize("kind", ["notes", "cards"])
    @pytest.mark.asyncio
    async def test_a_tombstone_for_an_existing_record_does_not_raise(
        self, two_devices, tmp_path, kind
    ):
        use = two_devices
        use("b")
        root = tmp_path / "sync"
        root.mkdir(parents=True)

        from open_notebook.sync import tombstones
        from open_notebook.sync.exchange import (
            _apply_tombstones,
            ensure_record_id,
            repo_query,
        )

        rid = f"{'note' if kind == 'notes' else 'flashcard'}:{uuid.uuid4().hex[:12]}"
        if kind == "notes":
            await repo_query(
                "CREATE $id CONTENT { title: $t, note_type: 'human', updated: time::now() }",
                {"id": ensure_record_id(rid), "t": "to be deleted"},
            )
        else:
            await repo_query(
                "CREATE $id CONTENT { guid: $g, front: 'q', back: 'a', updated: time::now() }",
                {"id": ensure_record_id(rid), "g": uuid.uuid4().hex},
            )

        tombstones.write_tombstone(
            root, rid, kind, previous_updated="2099-01-01T00:00:00+00:00"
        )
        result = {
            "tombstones_applied": 0,
            "tombstones_overridden_by_local_edit": 0,
            "tombstones_already_absent": 0,
            "errors": [],
        }

        await _apply_tombstones(root, result, dry_run=False)

        assert result["tombstones_applied"] == 1, "the record was held locally"
        assert result["errors"] == []

    @pytest.mark.asyncio
    async def test_a_source_tombstone_still_keeps_its_bytes(
        self, two_devices, tmp_path
    ):
        """The flag still has to reach the one delete that understands it."""
        use = two_devices
        use("b")
        root = tmp_path / "sync"
        root.mkdir(parents=True)

        from open_notebook.domain.notebook import Asset, Source
        from open_notebook.storage.blobs import blob_exists, store_bytes
        from open_notebook.sync import tombstones
        from open_notebook.sync.exchange import SOURCES_DIR, _apply_tombstones

        ref = store_bytes(b"the only copy of this file", "keepme.pdf")
        source = Source(
            title="t",
            topics=[],
            asset=Asset(
                sha256=ref.sha256, filename="keepme.pdf", size=len(b"the only copy")
            ),
        )
        await source.save()
        rid = str(source.id)

        tombstones.write_tombstone(
            root, rid, SOURCES_DIR, previous_updated="2099-01-01T00:00:00+00:00"
        )
        result = {
            "tombstones_applied": 0,
            "tombstones_overridden_by_local_edit": 0,
            "tombstones_already_absent": 0,
            "errors": [],
        }
        await _apply_tombstones(root, result, dry_run=False)

        assert result["tombstones_applied"] == 1
        assert blob_exists(ref.sha256), "a peer's delete must not destroy local bytes"
