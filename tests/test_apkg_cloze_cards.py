"""Cloze cards must survive .apkg export.

genanki 0.13.1 counts a cloze note's cards by scanning its text for Anki's
ordinal form, `{{c1::answer}}`, and nothing else. Its fallback for "no
ordinals found" is `if card_ords == {}` - a set compared against an empty
dict, which is always False - so that fallback is dead code.

Net effect: a note written as `{{answer}}` produces zero cards. The note
row is still written, so the package looks structurally correct, but Anki
has nothing to review and the card is silently gone.

This is the form the app actually stores: users type `{{answer}}`, and
that is what lands in `flashcard.cloze_text`. The pre-existing fixture in
test_apkg_export.py used `{{c1::1913}}` throughout, so it exercised the one
input genanki handles and never the one the app emits. That is how the
defect survived.

Observed on device: a 3-note deck (2 basic, 1 cloze) exported 3 notes and
2 cards. After the fix, 3 and 3.
"""

import io
import sqlite3
import zipfile

import pytest

from open_notebook.export.apkg import build_package_bytes, to_anki_cloze


def _card_counts(cards, deck_name="Study"):
    """(notes, cards) as they land in the collection database."""
    data = build_package_bytes(cards, deck_name=deck_name)
    zf = zipfile.ZipFile(io.BytesIO(data))
    db_bytes = zf.read("collection.anki2")
    conn = sqlite3.connect(":memory:")
    # sqlite3 cannot open a BytesIO, so load the dump into a temp file
    path = "/tmp/plusplus-cloze-card-counts.anki2"
    with open(path, "wb") as handle:
        handle.write(db_bytes)
    conn.close()
    conn = sqlite3.connect(path)
    try:
        return (
            conn.execute("SELECT count(*) FROM notes").fetchone()[0],
            conn.execute("SELECT count(*) FROM cards").fetchone()[0],
        )
    finally:
        conn.close()


class TestToAnkiCloze:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("uses expanding {{intervals}}.", "uses expanding {{c1::intervals}}."),
            ("{{a}} then {{b}}", "{{c1::a}} then {{c2::b}}"),
            ("{{a}} {{b}} {{c}}", "{{c1::a}} {{c2::b}} {{c3::c}}"),
            # an ordinal already present is kept, and numbering resumes after it
            ("{{c1::done}} plus {{fresh}}", "{{c1::done}} plus {{c2::fresh}}"),
            # Anki's hint form keeps its hint
            ("{{answer::a hint}}", "{{c1::answer::a hint}}"),
            ("no deletions at all", "no deletions at all"),
            ("", ""),
            ("{{unbalanced", "{{unbalanced"),
        ],
    )
    def test_ordinals_are_assigned_in_reading_order(self, raw, expected):
        assert to_anki_cloze(raw) == expected

    @pytest.mark.parametrize(
        "raw",
        [
            "{{a}} and {{b}}",
            "{{c1::x}} then {{y}}",
            "plain text",
            "{{answer::hint}}",
        ],
    )
    def test_conversion_is_idempotent(self, raw):
        """Re-exporting an already-converted string must not nest ordinals."""
        once = to_anki_cloze(raw)
        assert to_anki_cloze(once) == once


class TestClozeCardsReachThePackage:
    def test_short_form_cloze_produces_a_card(self):
        """The regression: `{{word}}` used to yield zero cards."""
        notes, cards = _card_counts(
            [
                {
                    "model": "cloze",
                    "front": None,
                    "back": None,
                    "cloze_text": "Spaced repetition uses expanding {{intervals}}.",
                    "tags": [],
                    "guid": "c" * 16,
                }
            ]
        )
        assert notes == 1
        assert cards == 1, (
            "a cloze card exported with zero cards: genanki needs the "
            "{{cN::...}} ordinal form and its own fallback is unreachable"
        )

    def test_each_deletion_becomes_its_own_card(self):
        notes, cards = _card_counts(
            [
                {
                    "model": "cloze",
                    "front": None,
                    "back": None,
                    "cloze_text": "{{alpha}} then {{beta}} then {{gamma}}",
                    "tags": [],
                    "guid": "d" * 16,
                }
            ]
        )
        assert notes == 1
        assert cards == 3

    def test_mixed_deck_exports_every_card(self):
        """The exact shape that came off the phone: 2 basic + 1 cloze."""
        notes, cards = _card_counts(
            [
                {
                    "model": "basic",
                    "front": "What is a lapse?",
                    "back": "A failed review.",
                    "cloze_text": None,
                    "tags": ["srs"],
                    "guid": "e" * 16,
                },
                {
                    "model": "basic",
                    "front": "What is a leech?",
                    "back": "A card failed repeatedly.",
                    "cloze_text": None,
                    "tags": ["srs"],
                    "guid": "f" * 16,
                },
                {
                    "model": "cloze",
                    "front": None,
                    "back": None,
                    "cloze_text": "Spaced repetition uses expanding {{intervals}}.",
                    "tags": ["srs"],
                    "guid": "1" * 16,
                },
            ]
        )
        assert (notes, cards) == (3, 3)
