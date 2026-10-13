"""App-to-app content exchange between two Open Notebook instances.

Scope, deliberately narrow:

    NOTES  full bidirectional, last-write-wins on `updated`
    CARDS  full bidirectional, including FSRS state, plus an append-only
           union of the review log

Two formats exist for two different jobs, and conflating them is the
mistake this module avoids:

  * JSON (here) is app-to-app sync. Lossless. It carries the fields that
    make a deck worth syncing.
  * `.apkg` is Anki interop. It **cannot** carry review scheduling -
    genanki hardcodes `due=0, ivl=0, type=0, queue=0` for every card, so
    a deck exported after a graded card comes back as a new card. Measured,
    not assumed: a card with `stability=2.3065` and a real `due` date
    exports to an Anki row with all four fields zero. Syncing cards via
    .apkg would therefore silently reset everyone's review history.
  * Markdown is Obsidian interop (see `api/routers/notes.py` export and
    `open_notebook/vault/ingest.py`). Useful to a human, lossy for us.

Sources are NOT synced here. They are ingested documents whose vectors
live outside the record, so a JSON copy would arrive with no embeddings
and no way to tell whether the original still exists. Files already have
a better transport: the vault ingest. Duplicating that here would create
orphaned embeddings and near-duplicate content.

Conflict policy, and its one acknowledged hole:

  * Note: the record with the later `updated` wins. Two devices editing
    the same note in the same instant lose one edit; `updated` is
    second-resolution and there is no merge.
  * Card content: last-write-wins, same rule.
  * Card schedule: the revlog is a **union**, deduplicated on
    (card_id, reviewed_at, rating, duration_ms), because review history
    is a fact that happened on both devices and must not be discarded.
    `fsrs_json` itself is last-write-wins on `last_review`.
    The hole: if both devices review the same card inside the same
    second, both revlog rows survive but only one FSRS state does, so the
    next interval reflects one of the two reviews, not both. Recovering
    would mean replaying the merged log through FSRS, which is a larger
    change than this slice takes on. It is a rare race and it loses
    schedule precision, not history.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from loguru import logger

from open_notebook.database.repository import repo_query

SCHEMA_VERSION = 1
MANIFEST = "manifest.json"
NOTES_DIR = "notes"
CARDS_DIR = "cards"
REVLOG = "revlog/revlog.jsonl"


def sync_dir() -> Path:
    """Where the exchange lives. A git repo, checked out wherever."""
    return Path(os.getenv("OPEN_NOTEBOOK_SYNC_DIR", "./sync"))


def _read_json(path: Path) -> Optional[Dict[str, Any]]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning(f"sync: unreadable {path.name}: {exc}")
        return None


def _write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    # Atomic: a half-written file in a git repo becomes a merge conflict on
    # the other device for no reason.
    tmp.replace(path)


# --------------------------------------------------------------------------
# export
# --------------------------------------------------------------------------


async def export_all(device: str, root: Optional[Path] = None) -> Dict[str, Any]:
    """Write every note, card and revlog row to `root`.

    Files are named from the record id, so re-exporting is idempotent and
    two devices exporting the same note produce the same filename and
    therefore a real git conflict rather than a silent duplicate.
    """
    from open_notebook.domain.flashcard import Flashcard
    from open_notebook.domain.notebook import Note

    root = root or sync_dir()
    (root / NOTES_DIR).mkdir(parents=True, exist_ok=True)
    (root / CARDS_DIR).mkdir(parents=True, exist_ok=True)

    notes: List[Note] = await Note.get_all()
    cards: List[Flashcard] = await Flashcard.get_all()

    for note in notes:
        _write_json(
            root / NOTES_DIR / f"{str(note.id).replace(':', '-')}.json",
            {
                "id": str(note.id),
                "title": note.title,
                "note_type": note.note_type,
                "content": note.content,
                "updated": note.updated.isoformat() if note.updated else None,
                "written_by": device,
            },
        )

    for card in cards:
        _write_json(
            root / CARDS_DIR / f"{str(card.id).replace(':', '-')}.json",
            {
                "id": str(card.id),
                "model": card.model,
                "front": card.front,
                "back": card.back,
                "cloze_text": card.cloze_text,
                "tags": list(card.tags or []),
                "guid": card.guid,
                "status": card.status,
                "source_id": card.source_id,
                "fsrs_json": card.fsrs_json,
                "updated": card.updated.isoformat() if card.updated else None,
                "written_by": device,
            },
        )

    revlog_rows = await repo_query(
        "SELECT card_id, rating, reviewed_at, duration_ms FROM revlog ORDER BY reviewed_at ASC"
    )
    revlog_path = root / REVLOG
    revlog_path.parent.mkdir(parents=True, exist_ok=True)
    revlog_path.write_text(
        "".join(
            json.dumps(r, default=str, sort_keys=True) + "\n"
            for r in (revlog_rows or [])
        ),
        encoding="utf-8",
    )

    manifest = {
        "schema_version": SCHEMA_VERSION,
        "device": device,
        "counts": {
            "notes": len(notes),
            "cards": len(cards),
            "revlog": len(revlog_rows or []),
        },
    }
    _write_json(root / MANIFEST, manifest)
    return manifest


# --------------------------------------------------------------------------
# import
# --------------------------------------------------------------------------


async def _maybe_get(cls, record_id: str):
    """Fetch a record, treating "not found" as None.

    `.get` raises for a missing record on every domain model here, and
    import has to distinguish that from a real error, so the try/except
    lives in one place instead of at each call site.
    """
    try:
        return await cls.get(record_id)
    except Exception:
        return None


def _newer(incoming: Optional[str], existing: Optional[str]) -> bool:
    """Whether an incoming `updated` should overwrite the local record."""
    if not incoming:
        return False
    if not existing:
        return True
    return incoming > existing


async def import_all(
    root: Optional[Path] = None, dry_run: bool = False
) -> Dict[str, Any]:
    """Upsert everything under `root` by stable id.

    Last-write-wins on `updated`; anything older than what we already hold
    is counted as `skipped_stale` rather than silently applied.
    """
    from open_notebook.domain.flashcard import Flashcard
    from open_notebook.domain.notebook import Note

    root = root or sync_dir()
    result: Dict[str, Any] = {
        "notes_created": 0,
        "notes_updated": 0,
        "notes_skipped_stale": 0,
        "cards_created": 0,
        "cards_updated": 0,
        "cards_skipped_stale": 0,
        "revlog_added": 0,
        "revlog_duplicates": 0,
        "errors": [],
        "dry_run": dry_run,
    }

    for path in sorted((root / NOTES_DIR).glob("*.json")):
        data = _read_json(path)
        if not data or not data.get("id"):
            result["errors"].append(f"{path.name}: no id")
            continue
        record_id = data["id"]
        existing_note: Optional[Note] = await _maybe_get(Note, record_id)

        local_updated = (
            existing_note.updated.isoformat()
            if (existing_note and existing_note.updated)
            else None
        )
        if existing_note and not _newer(data.get("updated"), local_updated):
            result["notes_skipped_stale"] += 1
            continue
        if dry_run:
            result["notes_created" if not existing_note else "notes_updated"] += 1
            continue

        note = existing_note or Note(
            id=record_id, title=data.get("title"), content=data.get("content") or "x"
        )
        note.title = data.get("title")
        note.content = data.get("content")
        note.note_type = data.get("note_type")
        await note.save()
        result["notes_updated" if existing_note else "notes_created"] += 1

    for path in sorted((root / CARDS_DIR).glob("*.json")):
        data = _read_json(path)
        if not data or not data.get("id"):
            result["errors"].append(f"{path.name}: no id")
            continue
        record_id = data["id"]
        existing_card: Optional[Flashcard] = await _maybe_get(Flashcard, record_id)

        local_updated = (
            existing_card.updated.isoformat()
            if (existing_card and existing_card.updated)
            else None
        )
        if existing_card and not _newer(data.get("updated"), local_updated):
            result["cards_skipped_stale"] += 1
            continue
        if dry_run:
            result["cards_created" if not existing_card else "cards_updated"] += 1
            continue

        card = existing_card or Flashcard(
            id=record_id,
            model=data.get("model") or "basic",
            status=data.get("status") or "draft",
        )
        card.model = data.get("model") or "basic"
        card.front = data.get("front")
        card.back = data.get("back")
        card.cloze_text = data.get("cloze_text")
        card.tags = list(data.get("tags") or [])
        card.guid = data.get("guid")
        card.status = data.get("status") or "draft"
        # source_id is deliberately NOT copied: the source table is not
        # synced, so the id would dangle on this device. A card with no
        # source still reviews and exports fine.
        card.fsrs_json = data.get("fsrs_json")
        await card.save()
        result["cards_updated" if existing_card else "cards_created"] += 1

    revlog_result = await _import_revlog(root, dry_run)
    result["revlog_added"] = revlog_result[0]
    result["revlog_duplicates"] = revlog_result[1]
    return result


def _revlog_key(row: Dict[str, Any]) -> Tuple[Any, str, str, Any]:
    """Identity of a review event.

    Single definition on purpose. An earlier version built the `seen` set
    and the per-row `key` from two separate but identical tuple
    expressions; changing one and not the other silently disables dedupe
    in one direction, and a test mutation slipped past because of it.
    """
    return (
        row.get("card_id"),
        str(row.get("rating")),
        str(row.get("reviewed_at")),
        row.get("duration_ms", 0),
    )


async def _import_revlog(root: Path, dry_run: bool) -> Tuple[int, int]:
    """Union the incoming review log against ours, deduped.

    Reviews are facts that occurred. Two devices each reviewed a card
    while offline; both rows are true and both are kept. Deduplication is
    on the full tuple, so a genuine repeat of the same rating is kept as
    long as it landed at a different time.
    """
    path = root / REVLOG
    if not path.exists():
        return 0, 0

    existing_rows = await repo_query(
        "SELECT card_id, rating, reviewed_at, duration_ms FROM revlog"
    )
    seen = {_revlog_key(r) for r in (existing_rows or [])}

    added = duplicates = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        key = _revlog_key(row)
        if key in seen:
            duplicates += 1
            continue
        seen.add(key)
        added += 1
        if dry_run:
            continue
        await repo_query(
            "CREATE revlog SET card_id = $card_id, rating = $rating, "
            "reviewed_at = $reviewed_at, duration_ms = $duration_ms",
            {
                "card_id": row.get("card_id"),
                "rating": row.get("rating"),
                "reviewed_at": row.get("reviewed_at"),
                "duration_ms": row.get("duration_ms", 0),
            },
        )
    return added, duplicates


def iter_sync_files(root: Optional[Path] = None) -> Iterable[Path]:
    root = root or sync_dir()
    yield from sorted((root / NOTES_DIR).glob("*.json"))
    yield from sorted((root / CARDS_DIR).glob("*.json"))
