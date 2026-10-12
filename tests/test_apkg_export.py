"""P6 exporter: structural shape of the .apkg (no live Anki here)."""

import sqlite3
import zipfile

from open_notebook.export.apkg import (
    DECK_ID,
    MODEL_BASIC_ID,
    MODEL_CLOZE_ID,
    build_package_bytes,
)


def _cards():
    return [
        {
            "model": "basic",
            "front": "Capital of France?",
            "back": "Paris",
            "cloze_text": None,
            "tags": ["geo"],
            "guid": "a" * 16,
        },
        {
            "model": "cloze",
            "front": None,
            "back": None,
            "cloze_text": "Canberra was founded in {{c1::1913}}",
            "tags": [],
            "guid": "b" * 16,
        },
    ]


def _open_package():
    import io

    data = build_package_bytes(_cards(), deck_name="Study")
    assert data[:2] == b"PK"
    zf = zipfile.ZipFile(io.BytesIO(data))
    assert "collection.anki2" in zf.namelist()
    db_bytes = zf.read("collection.anki2")
    db_path = "/tmp/plusplus-apkg-structural-test.anki2"
    with open(db_path, "wb") as f:
        f.write(db_bytes)
    return db_path


def test_package_has_frozen_deck_and_models():
    db_path = _open_package()
    conn = sqlite3.connect(db_path)
    try:
        decks = conn.execute("SELECT decks FROM col").fetchone()[0]
        models = conn.execute("SELECT models FROM col").fetchone()[0]
    finally:
        conn.close()
    import json

    decks = json.loads(decks)
    models = json.loads(models)
    assert any(
        d.get("id") == DECK_ID and d.get("name") == "Study" for d in decks.values()
    )
    assert str(MODEL_BASIC_ID) in models
    assert str(MODEL_CLOZE_ID) in models


def test_package_has_both_notes_with_guids():
    db_path = _open_package()
    conn = sqlite3.connect(db_path)
    try:
        notes = conn.execute("SELECT guid, flds FROM notes").fetchall()
    finally:
        conn.close()
    guids = [n[0] for n in notes]
    assert "a" * 16 in guids
    assert "b" * 16 in guids
    assert len(notes) == 2
