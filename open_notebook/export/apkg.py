"""Anki package exporter (P6). Frozen note models, stable GUIDs, no media v1.

Model/deck IDs are constants: additive `.apkg` re-imports dedupe on GUID +
stable model IDs instead of spawning duplicates. Media attachments are out
of scope for v1 (fields carry text only).
"""

from __future__ import annotations

import os
import tempfile
from typing import Any, Dict, List

# Frozen forever. New card types get NEW ids; these never change.
MODEL_BASIC_ID = 1607392319
MODEL_CLOZE_ID = 1607392320
DECK_ID = 1607392321


def _basic_model():
    import genanki

    return genanki.Model(
        MODEL_BASIC_ID,
        "OpenNotebook Basic",
        fields=[{"name": "Front"}, {"name": "Back"}],
        templates=[
            {
                "name": "Card 1",
                "qfmt": "{{Front}}",
                "afmt": '{{FrontSide}}\n\n<hr id="answer">\n\n{{Back}}',
            }
        ],
        css=".card { font-family: sans-serif; text-align: center; }",
    )


def _cloze_model():
    import genanki

    return genanki.Model(
        MODEL_CLOZE_ID,
        "OpenNotebook Cloze",
        fields=[{"name": "Text"}],
        templates=[
            {
                "name": "Cloze",
                "qfmt": "{{cloze:Text}}",
                "afmt": "{{cloze:Text}}",
            }
        ],
        css=".card { font-family: sans-serif; text-align: center; }",
        model_type=genanki.Model.CLOZE,
    )


def build_package_bytes(cards: List[Dict[str, Any]], deck_name: str) -> bytes:
    """Build a .apkg from approved-card dicts. Returns raw package bytes."""
    import genanki

    deck = genanki.Deck(DECK_ID, deck_name)
    models = {"basic": _basic_model(), "cloze": _cloze_model()}
    for card in cards:
        kind = card.get("model") or "basic"
        if kind == "cloze":
            fields = [card.get("cloze_text") or ""]
            note = genanki.Note(
                model=models["cloze"],
                fields=fields,
                guid=str(card.get("guid") or ""),
                tags=list(card.get("tags") or []),
            )
        else:
            note = genanki.Note(
                model=models["basic"],
                fields=[card.get("front") or "", card.get("back") or ""],
                guid=str(card.get("guid") or ""),
                tags=list(card.get("tags") or []),
            )
        deck.add_note(note)
    package = genanki.Package(deck)
    fd, path = tempfile.mkstemp(suffix=".apkg")
    os.close(fd)
    try:
        package.write_to_file(path)
        with open(path, "rb") as f:
            return f.read()
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
