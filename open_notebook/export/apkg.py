"""Anki package exporter (P6). Frozen note models, stable GUIDs, no media v1.

Model/deck IDs are constants: additive `.apkg` re-imports dedupe on GUID +
stable model IDs instead of spawning duplicates. Media attachments are out
of scope for v1 (fields carry text only).
"""

from __future__ import annotations

import os
import re
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


# Anki's storage form for a cloze deletion carries an explicit ordinal:
# `{{c1::answer}}`. Users write the shorter `{{answer}}`, and that is what we
# store. genanki 0.13.1 counts cards by scanning for the ordinal form only
# (note.py `_cloze_cards`), so `{{answer}}` yields *zero* cards and the card
# silently vanishes from the package - the note is written, so it looks
# fine, but Anki has nothing to review.
#
# That function also ends with `if card_ords == {}: card_ords = {0}`,
# which is a set compared to an empty dict and is therefore always False.
# The intended fallback for "no ordinals found" never runs, which is why the
# loss is total rather than partial.
_CLOZE = re.compile(r"\{\{(.*?)\}\}", re.DOTALL)
_ALREADY_ORDINED = re.compile(r"^c\d+::", re.IGNORECASE)


def to_anki_cloze(text: str) -> str:
    """Rewrite `{{x}}` deletions into Anki's `{{c1::x}}` ordinal form.

    Ordinals are assigned in reading order starting at 1, skipping any
    deletion that already has one, so re-exporting an already-converted
    string is a no-op rather than a second layer of `c1::c2::`.
    """
    out: List[str] = []
    ordinal = 0
    last = 0
    for match in _CLOZE.finditer(text):
        out.append(text[last : match.start()])
        body = match.group(1)
        existing = _ALREADY_ORDINED.match(body)
        if existing:
            # keep its own ordinal, and resync so later ones continue after it
            ordinal = max(ordinal, int(existing.group(0)[1:-2]))
            out.append(match.group(0))
        else:
            ordinal += 1
            out.append("{{c%d::%s}}" % (ordinal, body))
        last = match.end()
    out.append(text[last:])
    return "".join(out)


def build_package_bytes(cards: List[Dict[str, Any]], deck_name: str) -> bytes:
    """Build a .apkg from approved-card dicts. Returns raw package bytes."""
    import genanki

    deck = genanki.Deck(DECK_ID, deck_name)
    models = {"basic": _basic_model(), "cloze": _cloze_model()}
    for card in cards:
        kind = card.get("model") or "basic"
        if kind == "cloze":
            fields = [to_anki_cloze(card.get("cloze_text") or "")]
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
