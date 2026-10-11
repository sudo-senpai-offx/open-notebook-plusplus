"""P3 bank model: single table, status-driven lifecycle.

draft -> approved (human gate) -> tombstoned (delete). Tombstones exclude
from reviews/exports; Anki-side copies need manual deletion (honest copy).
GUIDs are content-stable so .apkg re-exports dedupe.
"""

from open_notebook.domain.flashcard import Flashcard, make_guid


def test_guid_stable_and_unique_per_index():
    assert make_guid("source:a", "cloze", 3) == make_guid("source:a", "cloze", 3)
    assert make_guid("source:a", "cloze", 3) != make_guid("source:a", "cloze", 4)
    assert make_guid("source:a", "basic", 3) != make_guid("source:b", "basic", 3)


def test_guid_is_compact_hex():
    guid = make_guid("source:a", "basic", 0)
    assert len(guid) == 16
    int(guid, 16)


def test_defaults_are_draft():
    card = Flashcard(model="basic", front="Q?", back="A.")
    assert card.status == "draft"
    assert card.tags == []
    assert card.source_id is None


def test_cloze_holds_text_not_sides():
    card = Flashcard(model="cloze", cloze_text="Canberra was founded in {{c1::1913}}")
    assert card.front is None
    assert card.guid is None or isinstance(card.guid, str)
