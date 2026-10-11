"""P3 bank model: single table, status-driven lifecycle.

draft -> approved (human gate) -> tombstoned (delete). Tombstones exclude
from reviews/exports; Anki-side copies need manual deletion (honest copy).
GUIDs are content-stable so .apkg re-exports dedupe.
"""

from open_notebook.domain.flashcard import Flashcard, content_guid


def test_guid_stable_and_content_addressed():
    a = content_guid("basic", "Q?", "A.")
    assert a == content_guid("basic", "Q?", "A.")
    assert a != content_guid("basic", "Q?", "B.")
    assert a != content_guid("cloze", "Q?", "A.")


def test_guid_is_compact_hex():
    guid = content_guid("basic", "Q?", "A.")
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
