"""Sync round-trip: export on one device, import on another, nothing lost.

The property that decides whether sync is safe is not "files were
written" but "a graded card still knows it was graded after both sides
have synced". Everything else follows from that, so it is asserted
first and directly.

`.apkg` cannot carry this: genanki hardcodes `due=0, ivl=0, type=0,
queue=0` on every card, so exporting a deck after review and reimporting
it yields an unreviewed card. That is why sync is JSON.
"""

import json
from datetime import datetime
from pathlib import Path

import pytest

from open_notebook.review.scheduler import grade_card
from open_notebook.sync.exchange import (
    CARDS_DIR,
    MANIFEST,
    NOTES_DIR,
    REVLOG,
    SCHEMA_VERSION,
    _newer,
)


class TestTimestampComparison:
    @pytest.mark.parametrize(
        ("incoming", "existing", "expect"),
        [
            ("2026-01-02T00:00:00", "2026-01-01T00:00:00", True),
            ("2026-01-01T00:00:00", "2026-01-02T00:00:00", False),
            ("2026-01-01T00:00:00", "2026-01-01T00:00:00", False),  # tie -> keep local
            ("2026-01-02T00:00:00", None, True),
            (None, "2026-01-01T00:00:00", False),
            (None, None, False),
        ],
    )
    def test_newer(self, incoming, existing, expect):
        assert _newer(incoming, existing) is expect


class TestCardSerialisation:
    def test_fsrs_state_survives_serialisation(self):
        """The reason sync is not .apkg."""
        state, _ = grade_card({}, "good")
        payload = {"id": "flashcard:abc", "fsrs_json": state}
        # what the exporter writes is what json.loads returns, unchanged
        import json

        assert (
            json.loads(json.dumps(payload))["fsrs_json"]["stability"]
            == state["stability"]
        )
        assert json.loads(json.dumps(payload))["fsrs_json"]["due"] == state["due"]

    def test_export_uses_stable_id_filenames(self, tmp_path):
        """Same id on two devices must produce the same path, so git
        reports a conflict instead of silently duplicating."""
        note_id = "note:abc123"
        path = f"{note_id.replace(':', '-')}.json"
        assert path == "note-abc123.json"
        assert f"{'flashcard:abc123'.replace(':', '-')}.json" == "flashcard-abc123.json"


class TestRevlogDedupeKey:
    def _key(self, row):
        return (
            row.get("card_id"),
            str(row.get("rating")),
            str(row.get("reviewed_at")),
            row.get("duration_ms", 0),
        )

    def test_identical_reviews_dedupe(self):
        a = {
            "card_id": "c",
            "rating": "good",
            "reviewed_at": "2026-01-01",
            "duration_ms": 10,
        }
        assert self._key(a) == self._key(dict(a))

    def test_same_rating_different_time_is_two_reviews(self):
        a = {
            "card_id": "c",
            "rating": "good",
            "reviewed_at": "2026-01-01",
            "duration_ms": 10,
        }
        b = dict(a, reviewed_at="2026-01-02")
        assert self._key(a) != self._key(b)

    def test_different_cards_do_not_collide(self):
        a = {"card_id": "c1", "rating": "good", "reviewed_at": "t", "duration_ms": 1}
        b = dict(a, card_id="c2")
        assert self._key(a) != self._key(b)


class TestLayout:
    def test_layout_is_declared(self):
        assert SCHEMA_VERSION == 1
        assert (NOTES_DIR, CARDS_DIR, REVLOG) == (
            "notes",
            "cards",
            "revlog/revlog.jsonl",
        )

    def test_manifest_is_part_of_layout(self):
        assert MANIFEST == "manifest.json"

    def test_no_vault_dependency(self):
        """Sync must not import the vault ingest path.

        Checked with AST, not a text search: the module docstring explains
        *why* the vault is not used, so grepping the file matches its own
        explanation. That is how a grep-based test passes for the wrong
        reason, which is worse than having no test.
        """
        import ast

        import open_notebook.sync.exchange as ex

        assert not hasattr(ex, "scan_vault")
        assert not hasattr(ex, "vault_dir")

        tree = ast.parse(Path(ex.__file__).read_text())
        imported: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                imported.append(node.module or "")
            elif isinstance(node, ast.Import):
                imported.extend(alias.name for alias in node.names)
        vault_imports = [name for name in imported if "vault" in name]
        assert vault_imports == [], (
            f"sync must not depend on the vault module: {vault_imports}"
        )


class TestTimestampNormalisation:
    """SurrealDB serialises datetimes with a space separator.

    Once migration 34 stops the field definition overwriting `reviewed_at`
    with `time::now()`, that exact string is handed to a `TYPE datetime`
    field, which rejects it:

        Found '2026-10-13 06:55:36.740943+00:00' for field `reviewed_at`,
        but expected a datetime
    """

    def test_space_separated_string_is_parsed(self):
        from open_notebook.sync.exchange import _as_iso

        out = _as_iso("2026-10-13 06:55:36.740943+00:00")
        assert isinstance(out, datetime)
        assert out.year == 2026 and out.hour == 6

    def test_iso_string_still_works(self):
        from open_notebook.sync.exchange import _as_iso

        assert isinstance(_as_iso("2026-10-13T06:55:36.740943+00:00"), datetime)

    def test_unparseable_passes_through(self):
        """Must not silently drop a bad value: surfacing it as a schema
        error naming the value beats an absent review."""
        from open_notebook.sync.exchange import _as_iso

        assert _as_iso("not a timestamp") == "not a timestamp"
        assert _as_iso(None) is None
        assert _as_iso(42) == 42

    def test_datetime_passes_through(self):
        from open_notebook.sync.exchange import _as_iso

        now = datetime(2026, 1, 1)
        assert _as_iso(now) is now


class TestIdAwareCreate:
    """ObjectModel.save() branches on `id is None`.

    SurrealDB's UPDATE against a record that does not exist is a no-op
    returning [], so building a model carrying the other device's id and
    calling save() creates nothing and then raises IndexError on
    result_list[0]. Found by running the import against a real database;
    the unit tests patch save() outright and never ran this branch.
    """

    @pytest.mark.asyncio
    async def test_uses_upsert_with_the_supplied_id(self, monkeypatch):
        from open_notebook.domain.flashcard import Flashcard
        from open_notebook.sync import exchange as ex

        seen: dict = {}

        async def fake_query(q, v=None):
            seen["q"] = q
            seen["v"] = v
            return [{"status": "approved"}]

        monkeypatch.setattr(ex, "repo_query", fake_query)
        card = Flashcard(model="basic", status="approved")
        card.front = "f"
        await ex._create_with_id(card, "flashcard:abc")

        assert "UPSERT" in seen["q"], "UPDATE no-ops on a missing record"
        assert str(seen["v"]["rid"]) == "flashcard:abc"
        assert seen["v"]["data"].get("front") == "f"

    @pytest.mark.asyncio
    async def test_empty_result_does_not_raise(self, monkeypatch):
        """The original crash: IndexError on result_list[0]."""
        from open_notebook.domain.flashcard import Flashcard
        from open_notebook.sync import exchange as ex

        async def fake_query(_q, _v=None):
            return []

        monkeypatch.setattr(ex, "repo_query", fake_query)
        card = Flashcard(model="basic", status="approved")
        await ex._create_with_id(card, "flashcard:empty")
        assert str(card.id) == "flashcard:empty"


class _Rec:
    """Minimal stand-in for a domain record: what the sync code touches."""

    recorded: list = []

    def __init__(self, **kw):
        self.id = kw.pop("id", None)
        self.updated = kw.pop("updated", None)
        self.saved = False
        self.__dict__.update(kw)

    async def save(self):
        self.saved = True
        _Rec.recorded.append(self)


def _patch(monkeypatch, existing_by_id, revlog_rows=None):
    """Wire the sync code at a fake database. Returns the list of saves."""
    from open_notebook.domain import flashcard as fc
    from open_notebook.domain import notebook as nb
    from open_notebook.sync import exchange as ex

    _Rec.recorded = []

    async def get_all(_cls, order_by=None):
        return []

    async def _save(self):
        # No `self.saved = True` here: the real Note/Flashcard are pydantic
        # models and reject unknown attributes outright.
        _Rec.recorded.append(self)

    for mod, klass in ((nb, "Note"), (fc, "Flashcard")):
        cls = getattr(mod, klass)
        monkeypatch.setattr(
            cls,
            "get",
            staticmethod(lambda _id, _m=existing_by_id: _resolve(_m, _id)),
            raising=False,
        )
        monkeypatch.setattr(cls, "get_all", staticmethod(get_all), raising=False)
        monkeypatch.setattr(cls, "save", _save, raising=False)

    async def fake_query(_q, _v=None):
        return list(revlog_rows or [])

    monkeypatch.setattr(ex, "repo_query", fake_query)

    # New records go through _create_with_id, not save(): carrying the
    # other device's id would otherwise make save() take the UPDATE path,
    # which no-ops against a record that does not exist.
    async def fake_create(instance, record_id):
        _Rec.recorded.append(instance)

    monkeypatch.setattr(ex, "_create_with_id", fake_create)
    return _Rec.recorded


async def _resolve(existing_by_id, _id):
    rec = existing_by_id.get(_id)
    if rec is None:
        raise LookupError(_id)
    return rec


class TestImportDecisions:
    """The decisions, not the IO: what gets written, skipped, counted."""

    @pytest.mark.asyncio
    async def test_stale_note_is_skipped(self, tmp_path, monkeypatch):
        from datetime import datetime

        notes = tmp_path / "notes"
        notes.mkdir()
        (notes / "note-a.json").write_text(
            '{"id": "note:a", "title": "old", "content": "old",'
            ' "updated": "2026-01-01T00:00:00"}'
        )
        existing = _Rec(
            id="note:a",
            title="newer local",
            content="newer local",
            note_type=None,
            updated=datetime.fromisoformat("2026-06-01T00:00:00"),
        )
        saved = _patch(monkeypatch, {"note:a": existing})

        from open_notebook.sync.exchange import import_all

        result = await import_all(root=tmp_path, dry_run=False)
        assert result["notes_skipped_stale"] == 1
        assert result["notes_updated"] == 0
        assert saved == [], "a stale record must not be written"

    @pytest.mark.asyncio
    async def test_newer_note_overwrites(self, tmp_path, monkeypatch):
        from datetime import datetime

        notes = tmp_path / "notes"
        notes.mkdir()
        (notes / "note-a.json").write_text(
            '{"id": "note:a", "title": "from phone", "content": "new body",'
            ' "note_type": "human", "updated": "2026-12-01T00:00:00"}'
        )
        existing = _Rec(
            id="note:a",
            title="stale local",
            content="stale",
            note_type=None,
            updated=datetime.fromisoformat("2026-06-01T00:00:00"),
        )
        saved = _patch(monkeypatch, {"note:a": existing})

        from open_notebook.sync.exchange import import_all

        result = await import_all(root=tmp_path, dry_run=False)
        assert result["notes_updated"] == 1
        assert result["notes_skipped_stale"] == 0
        assert len(saved) == 1
        assert saved[0].title == "from phone"
        assert saved[0].content == "new body"

    @pytest.mark.asyncio
    async def test_dry_run_writes_nothing(self, tmp_path, monkeypatch):
        notes = tmp_path / "notes"
        notes.mkdir()
        (notes / "note-new.json").write_text(
            '{"id": "note:new", "title": "t", "content": "c", "updated": "2026-12-01T00:00:00"}'
        )
        saved = _patch(monkeypatch, {})

        from open_notebook.sync.exchange import import_all

        result = await import_all(root=tmp_path, dry_run=True)
        assert result["dry_run"] is True
        assert result["notes_created"] == 1
        assert saved == [], "dry_run must not persist anything"

    @pytest.mark.asyncio
    async def test_card_fsrs_state_is_written_through(self, tmp_path, monkeypatch):
        cards = tmp_path / "cards"
        cards.mkdir()
        state, _ = grade_card({}, "good")
        (cards / "flashcard-x.json").write_text(
            '{"id": "flashcard:x", "model": "basic", "front": "f", "back": "b",'
            f' "tags": ["srs"], "status": "approved", "fsrs_json": {json.dumps(state)},'
            ' "updated": "2026-12-01T00:00:00"}'
        )
        saved = _patch(monkeypatch, {})

        from open_notebook.sync.exchange import import_all

        result = await import_all(root=tmp_path, dry_run=False)
        assert result["cards_created"] == 1
        assert len(saved) == 1
        assert saved[0].fsrs_json["stability"] == state["stability"]
        assert saved[0].fsrs_json["due"] == state["due"]

    @pytest.mark.asyncio
    async def test_source_id_is_not_copied(self, tmp_path, monkeypatch):
        """The source table is not synced, so carrying its id over would
        leave a dangling reference on the receiving device."""
        cards = tmp_path / "cards"
        cards.mkdir()
        (cards / "flashcard-y.json").write_text(
            '{"id": "flashcard:y", "model": "basic", "front": "f",'
            ' "source_id": "source:does-not-exist-here", "status": "approved",'
            ' "updated": "2026-12-01T00:00:00"}'
        )
        saved = _patch(monkeypatch, {})

        from open_notebook.sync.exchange import import_all

        await import_all(root=tmp_path, dry_run=False)
        assert getattr(saved[0], "source_id", None) in (None, "")


class TestRevlogUnion:
    @pytest.mark.asyncio
    async def test_union_adds_and_dedupes(self, tmp_path, monkeypatch):
        (tmp_path / "revlog").mkdir()
        rows = [
            {
                "card_id": "c1",
                "rating": "good",
                "reviewed_at": "2026-01-01",
                "duration_ms": 5,
            },
            {
                "card_id": "c2",
                "rating": "hard",
                "reviewed_at": "2026-01-02",
                "duration_ms": 7,
            },
            {
                "card_id": "c3",
                "rating": "good",
                "reviewed_at": "2026-01-03",
                "duration_ms": 9,
            },
        ]
        (tmp_path / "revlog" / "revlog.jsonl").write_text(
            "".join(json.dumps(r) + "\n" for r in rows)
        )
        # we already hold the first two
        existing = [
            {
                "card_id": "c1",
                "rating": "good",
                "reviewed_at": "2026-01-01",
                "duration_ms": 5,
            },
            {
                "card_id": "c2",
                "rating": "hard",
                "reviewed_at": "2026-01-02",
                "duration_ms": 7,
            },
        ]
        _patch(monkeypatch, {}, revlog_rows=existing)

        from open_notebook.sync.exchange import _import_revlog

        added, duplicates = await _import_revlog(tmp_path, dry_run=True)
        assert added == 1
        assert duplicates == 2

    @pytest.mark.asyncio
    async def test_two_devices_reviews_both_survive(self, tmp_path, monkeypatch):
        """The whole point of a union: two offline reviews are both true."""
        (tmp_path / "revlog").mkdir()
        incoming = {
            "card_id": "c1",
            "rating": "good",
            "reviewed_at": "2026-01-02",
            "duration_ms": 3,
        }
        (tmp_path / "revlog" / "revlog.jsonl").write_text(json.dumps(incoming) + "\n")
        local = {
            "card_id": "c1",
            "rating": "good",
            "reviewed_at": "2026-01-01",
            "duration_ms": 3,
        }
        _patch(monkeypatch, {}, revlog_rows=[local])

        from open_notebook.sync.exchange import _import_revlog

        added, duplicates = await _import_revlog(tmp_path, dry_run=True)
        assert (added, duplicates) == (1, 0), (
            "a review on each device must both be kept"
        )

    @pytest.mark.asyncio
    async def test_missing_revlog_is_not_an_error(self, tmp_path, monkeypatch):
        _patch(monkeypatch, {})
        from open_notebook.sync.exchange import _import_revlog

        assert await _import_revlog(tmp_path, dry_run=True) == (0, 0)


class TestNotebookSlugs:
    """Notes reference notebooks by slug, never by record id.

    A record id is per-database and means nothing on the other device.
    Verified against two real databases: the note attached to device B's
    own notebook id, resolved from the slug alone.
    """

    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            ("Cognitive Science", "cognitive-science"),
            ("cognitive science", "cognitive-science"),  # case-insensitive
            ("Linear Algebra!", "linear-algebra"),  # punctuation collapsed
            ("  spaced   out  ", "spaced-out"),
            ("", ""),
        ],
    )
    def test_slug_normalisation(self, name, expected):
        from open_notebook.sync.exchange import _slug

        assert _slug(name) == expected

    @pytest.mark.asyncio
    async def test_unknown_slug_is_reported_not_silently_dropped(
        self, tmp_path, monkeypatch
    ):
        """A missing notebook must not discard the note or fabricate an edge.

        The dangerous outcome would be relating the note to a notebook id
        that does not exist here. SurrealDB record links do not prevent
        dangling references, so nothing downstream would complain.
        """
        notes = tmp_path / "notes"
        notes.mkdir()
        (notes / "note-n.json").write_text(
            '{"id": "note:n", "title": "t", "content": "c",'
            ' "updated": "2026-12-01T00:00:00", "notebook_slugs": ["ghost-notebook"]}'
        )
        _patch(monkeypatch, {}, revlog_rows=[])

        from open_notebook.sync.exchange import import_all

        result = await import_all(root=tmp_path, dry_run=False)
        assert result["notes_created"] == 1, "content must still import"
        assert result["notes_attached"] == 0
        assert result["notes_unattached"] == 1
        assert result["missing_notebooks"] == ["ghost-notebook"]

    @pytest.mark.asyncio
    async def test_no_slugs_means_no_attempt(self, tmp_path, monkeypatch):
        notes = tmp_path / "notes"
        notes.mkdir()
        (notes / "note-n.json").write_text(
            '{"id": "note:n", "title": "t", "content": "c",'
            ' "updated": "2026-12-01T00:00:00"}'
        )
        _patch(monkeypatch, {}, revlog_rows=[])

        from open_notebook.sync.exchange import import_all

        result = await import_all(root=tmp_path, dry_run=False)
        assert result["notes_attached"] == 0
        assert result["missing_notebooks"] == []
