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

Sources sync as metadata plus a content hash; the bytes travel over the
blob channel and are keyed by that hash. Embeddings and chunk text are
never transported - they are recomputed from `full_text` with whatever
embedding model the receiving device runs. See `export_all` for why
excluding sources outright was the wrong call.

Deletions are tombstoned. Absence is not information, so a record that
went away is written down explicitly and resolved by the same
last-write-wins rule as everything else. See `sync.tombstones`.

Conflict policy, and its one acknowledged hole:

  * Note: the record with the later `updated` wins. Two devices editing
    the same note in the same instant lose one edit; `updated` is
    second-resolution and there is no merge.
  * Deletion: a tombstone, compared by the same rule. Newer tombstone
    deletes the record; a record edited after the delete comes back.
    Delete is a write like any other, not a permanent verdict.
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

import hashlib
import json
import os
import re
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from loguru import logger
from pydantic import BaseModel

from open_notebook.database.repository import ensure_record_id, repo_query
from open_notebook.domain.notebook import Asset, Source
from open_notebook.storage.blobs import resolve_blob_path
from open_notebook.sync import tombstones

# Bumped to 2 when sources entered the manifest. A v1 repo simply has no
# `sources/` directory, which imports as "no sources" rather than an
# error, so an older repo stays readable and a newer one is simply
# ignored by an older build.
SCHEMA_VERSION = 2
MANIFEST = "manifest.json"
NOTES_DIR = "notes"
NOTEBOOKS = "notebooks/index.json"
CARDS_DIR = "cards"
SOURCES_DIR = "sources"
REVLOG = "revlog/revlog.jsonl"


def sync_dir() -> Path:
    """Where the exchange lives. A git repo, checked out wherever."""
    return Path(os.getenv("OPEN_NOTEBOOK_SYNC_DIR", "./sync"))


def _slug(name: str) -> str:
    """Device-independent key for a notebook name.

    Record ids cannot cross devices - they are per-database. A name is
    the only thing both sides agree on, so it is normalised into a slug
    and matched case-insensitively on import.

    Unicode-aware, because an ASCII-only rule silently erases non-Latin
    notebooks. `[^a-z0-9]+` turned '日本語勉強会   ||   アドバンス' into
    the empty string, which is then filtered out: the notebook vanished
    from the index and every note attached to it exported with an empty
    `notebook_slugs`. That is real data loss with no error anywhere -
    found by exporting the operator's own PC database, where two of two
    notes happened to hang off exactly that notebook.

    `\\w` is Unicode-aware in Python 3, so CJK and accented characters
    survive. NFKC folds compatibility forms (full-width and half-width
    lookalikes) so they match, and casefold beats lower for matching.
    A name with no word characters at all falls back to a hash, which is
    still stable across devices and still distinct.
    """
    normalised = unicodedata.normalize("NFKC", name or "").casefold()
    cleaned = re.sub(r"\W+", "-", normalised).strip("-")
    if not cleaned:
        digest = hashlib.sha1((name or "").encode("utf-8")).hexdigest()[:8]
        cleaned = f"notebook-{digest}"
    return cleaned


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
    """Write every note, source, card and revlog row to `root`.

    Files are named from the record id, so re-exporting is idempotent and
    two devices exporting the same note produce the same filename and
    therefore a real git conflict rather than a silent duplicate.

    The payload carries ONLY fields sync owns. `source_id` and
    `written_by` were both dropped because each one flip-flopped forever:

      * source_id is not imported (sources are not synced), so the phone
        always exports null while the PC always exports its real id.
      * written_by is stamped with the exporting device, so the two
        devices overwrote each other's value on every cycle.

    Provenance lives in the git commit instead - the author and message
    already say which device pushed. Keeping either field produced a
    commit per cycle, which is churn by another name.

    ### Sources carry a hash, never bytes

    A source entry is metadata plus `sha256`. Three things are
    deliberately absent, and each is regenerable on the receiving device
    rather than transported:

      * the file bytes - delivered by the blob channel, keyed by hash;
      * `full_text` - re-extracted from the file once it arrives;
      * embeddings and chunk text - recomputed from `full_text` with the
        local embedding model.

    That last point is why sources can sync at all. The earlier reason
    for excluding them - "vectors live outside the record" - was wrong:
    `source_embedding` holds the vector and the chunk text, both inside
    the database. Neither should ever be synced. The real blocker was
    that a synced source arrived with no file behind it, and a hash plus
    a blob transfer solves exactly that.

    A source whose file is still on the legacy uploads path exports with
    `sha256: null`, since nothing has hashed it yet. It still imports as
    metadata; only the bytes are missing.
    """
    from open_notebook.domain.flashcard import Flashcard
    from open_notebook.domain.notebook import Note

    root = root or sync_dir()
    (root / NOTES_DIR).mkdir(parents=True, exist_ok=True)
    (root / CARDS_DIR).mkdir(parents=True, exist_ok=True)
    (root / SOURCES_DIR).mkdir(parents=True, exist_ok=True)

    notes: List[Note] = await Note.get_all()
    cards: List[Flashcard] = await Flashcard.get_all()
    sources: List[Source] = await Source.get_all()

    # Notebooks are referenced by slug, not id: a record id is per-database
    # and means nothing on the other device. One query each rather than
    # one per note.
    notebook_names = {
        str(r["id"]): r.get("name") or ""
        for r in (await repo_query("SELECT id, name FROM notebook") or [])
    }
    note_to_notebooks: Dict[str, List[str]] = {}
    for edge in await repo_query("SELECT in, out FROM artifact") or []:
        if edge.get("in"):
            note_to_notebooks.setdefault(str(edge["in"]), []).append(str(edge["out"]))

    slugs_for = {
        nid: sorted(
            {
                _slug(notebook_names.get(nbid, ""))
                for nbid in nbids
                if _slug(notebook_names.get(nbid, ""))
            }
        )
        for nid, nbids in note_to_notebooks.items()
    }

    _write_json(
        root / NOTEBOOKS,
        {
            "notebooks": sorted(
                (
                    {"slug": _slug(name), "name": name}
                    for name in notebook_names.values()
                ),
                key=lambda nb: nb["slug"],
            )
        },
    )

    for note in notes:
        _write_json(
            root / NOTES_DIR / f"{str(note.id).replace(':', '-')}.json",
            {
                "id": str(note.id),
                "title": note.title,
                "note_type": note.note_type,
                "content": note.content,
                "updated": note.updated.isoformat() if note.updated else None,
                "notebook_slugs": slugs_for.get(str(note.id), []),
            },
        )

    for source in sources:
        asset = source.asset or Asset()
        # A source can hang off a notebook by two edge types depending on
        # how it was added - sources use `reference`, notes `artifact`.
        # Read both so a source's grouping survives the round trip
        # whichever path put it there.
        slugs: set[str] = set()
        for edge_type in ("artifact", "reference"):
            edges = (
                await repo_query(
                    f"SELECT out FROM {edge_type} WHERE in = $sid",  # noqa: S608
                    {"sid": ensure_record_id(str(source.id))},
                )
                or []
            )
            for edge in edges:
                slug = _slug(notebook_names.get(str(edge.get("out")), ""))
                if slug:
                    slugs.add(slug)

        _write_json(
            root / SOURCES_DIR / f"{str(source.id).replace(':', '-')}.json",
            {
                "id": str(source.id),
                "title": source.title,
                "topics": list(source.topics or []),
                "updated": source.updated.isoformat() if source.updated else None,
                # kind lets the receiver tell "needs bytes fetched" from
                # "is a link, nothing to fetch".
                "kind": _source_kind(asset),
                "url": asset.url,
                "sha256": asset.sha256,
                "filename": asset.filename,
                "size": asset.size,
                "notebook_slugs": sorted(slugs),
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
                "fsrs_json": card.fsrs_json,
                "updated": card.updated.isoformat() if card.updated else None,
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

    # Deletions are detected here rather than in each delete method.
    # Comparing the manifest against what exists now catches every route
    # that removes a record - the UI endpoint, the notebook cascade, a
    # script - including ones nobody remembered to instrument. Poking
    # the delete methods would cover the ones already known about and
    # miss the next one silently.
    removed = tombstones.sweep(
        root,
        {
            NOTES_DIR: [str(n.id) for n in notes],
            CARDS_DIR: [str(c.id) for c in cards],
            SOURCES_DIR: [str(s.id) for s in sources],
        },
    )

    manifest = {
        "schema_version": SCHEMA_VERSION,
        "device": device,
        "counts": {
            "notes": len(notes),
            "cards": len(cards),
            "sources": len(sources),
            "revlog": len(revlog_rows or []),
            "notebooks": len(notebook_names),
            "tombstoned": len(removed),
        },
        # Sources whose bytes this device does not hold. The blob channel
        # uses it to decide what to ask for; it is informational in git.
        "sources_awaiting_blobs": sum(
            1
            for s in sources
            if (
                s.asset
                and s.asset.sha256
                and not resolve_blob_path(s.asset.sha256, s.asset.filename)
            )
        ),
    }
    _write_json(root / MANIFEST, manifest)
    return manifest


def _source_kind(asset: Asset) -> str:
    """Classify a source so the receiver knows whether to fetch bytes.

    A file source needs a blob transfer. A link and a text source do
    not: the receiver already holds everything that defines them.
    """
    if asset.sha256 or asset.file_path:
        return "file"
    if asset.url:
        return "link"
    return "text"


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


async def _create_with_id(instance, record_id: str) -> None:
    """Create a domain record under a caller-supplied id.

    Needed because `ObjectModel.save()` branches on `self.id is None`, and
    SurrealDB's `UPDATE` against a record that does not exist is a no-op
    returning `[]` - so building a model with the *other* device's id and
    calling `save()` silently creates nothing, then raises IndexError on
    `result_list[0]`. `repo_create` cannot cover this either: it pops
    `id`.

    Both were found by running the import against a real SurrealDB. The
    unit tests patch `save()` outright, so the create/update branch was
    never executed by any of them.

    UPSERT rather than CREATE so a concurrent import from the other
    device cannot turn a duplicate into an error.
    """
    data = instance._prepare_save_data()
    data.pop("id", None)
    rows = await repo_query(
        "UPSERT $rid MERGE $data RETURN AFTER",
        {"rid": ensure_record_id(record_id), "data": data},
    )
    row = (rows or [{}])[0] or {}
    for key, value in row.items():
        if key == "id" or not hasattr(instance, key):
            continue
        current = getattr(instance, key)
        # A nested model (Source.asset) must not be replaced by the raw
        # dict the database returned. setattr does not re-validate, so the
        # instance would carry a plain dict where callers expect
        # `asset.sha256`. Found by the live round-trip test; latent for
        # notes and cards too, which never happened to do attribute
        # access on a nested field.
        if isinstance(current, BaseModel) and isinstance(value, dict):
            value = type(current)(**value)
        setattr(instance, key, value)
    instance.id = ensure_record_id(record_id)


async def _apply_timestamp(instance, incoming: Optional[str]) -> None:
    """Carry the other device's `updated` onto the saved record.

    Migration 35 removed the `VALUE time::now()` clause that made this
    impossible, but the schema change alone did nothing: the importer was
    never *supplying* a value, only reading one for the last-write-wins
    comparison. Two writes rather than one, because `ObjectModel.save()`
    unconditionally stamps `updated` with the current time, so the
    incoming value has to be restored after it.

    Only reached when the incoming timestamp was already judged newer, so
    this cannot move a record backwards.
    """
    if not incoming:
        return
    stamp = _as_iso(incoming)
    try:
        await repo_query(
            "UPDATE $rid SET updated = $updated",
            {"rid": ensure_record_id(str(instance.id)), "updated": stamp},
        )
        instance.updated = stamp
    except Exception as exc:  # noqa: BLE001 - never fail an import on this
        logger.warning(f"sync: could not set updated on {instance.id}: {exc}")


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
    from open_notebook.domain.notebook import Note, Source

    root = root or sync_dir()

    local_notebooks = {
        _slug(r.get("name") or ""): str(r["id"])
        for r in (await repo_query("SELECT id, name FROM notebook") or [])
    }
    missing: set[str] = set()

    result: Dict[str, Any] = {
        "notes_created": 0,
        "notes_updated": 0,
        "notes_skipped_stale": 0,
        "cards_created": 0,
        "cards_updated": 0,
        "cards_skipped_stale": 0,
        "sources_created": 0,
        "sources_updated": 0,
        "sources_skipped_stale": 0,
        # Sources whose metadata landed but whose bytes are not on this
        # device yet. Reported rather than hidden: an imported source
        # that cannot be opened is not the same as one that failed, and
        # the operator needs to know a blob transfer is outstanding.
        "sources_awaiting_blobs": 0,
        "notes_attached": 0,
        "notes_unattached": 0,
        "missing_notebooks": [],
        "revlog_added": 0,
        "revlog_duplicates": 0,
        # Deletions carried by the peer. `overridden_by_local_edit` is
        # the record coming back because this device edited it after the
        # delete - correct, and worth seeing rather than hiding.
        "tombstones_applied": 0,
        "tombstones_overridden_by_local_edit": 0,
        "tombstones_already_absent": 0,
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
            title=data.get("title"), content=data.get("content") or "x"
        )
        note.title = data.get("title")
        note.content = data.get("content")
        note.note_type = data.get("note_type")
        if existing_note:
            await note.save()
        else:
            await _create_with_id(note, record_id)
        await _apply_timestamp(note, data.get("updated"))
        result["notes_updated" if existing_note else "notes_created"] += 1

        slugs = data.get("notebook_slugs") or []
        if slugs:
            linked = (
                await repo_query(
                    "SELECT VALUE out FROM artifact WHERE in = $nid",
                    {"nid": ensure_record_id(record_id)},
                )
                or []
            )
            already = {str(x) for x in linked}
            attached = False
            for slug in slugs:
                nb_id = local_notebooks.get(slug)
                if not nb_id:
                    missing.add(slug)
                    continue
                if nb_id in already:
                    attached = True
                    continue
                if dry_run:
                    attached = True
                    continue
                try:
                    await note.add_to_notebook(nb_id)
                    attached = True
                except Exception as exc:  # noqa: BLE001 - one bad edge is not fatal
                    logger.warning(f"sync: could not attach {record_id}: {exc}")
            if attached:
                result["notes_attached"] += 1
            else:
                result["notes_unattached"] += 1

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
        if existing_card:
            await card.save()
        else:
            await _create_with_id(card, record_id)
        await _apply_timestamp(card, data.get("updated"))
        result["cards_updated" if existing_card else "cards_created"] += 1

    for path in sorted((root / SOURCES_DIR).glob("*.json")):
        data = _read_json(path)
        if not data or not data.get("id"):
            result["errors"].append(f"{path.name}: no id")
            continue
        record_id = data["id"]
        existing_source: Optional[Source] = await _maybe_get(Source, record_id)

        local_updated = (
            existing_source.updated.isoformat()
            if (existing_source and existing_source.updated)
            else None
        )
        if existing_source and not _newer(data.get("updated"), local_updated):
            result["sources_skipped_stale"] += 1
            # Still count an outstanding blob: staleness is about the
            # metadata, not about whether this device can open the file.
            if _needs_blob(existing_source):
                result["sources_awaiting_blobs"] += 1
            continue
        if dry_run:
            result["sources_created" if not existing_source else "sources_updated"] += 1
            if data.get("sha256") and not _blob_present(data):
                result["sources_awaiting_blobs"] += 1
            continue

        # file_path is deliberately NOT taken from the manifest. It is a
        # path on the *sending* device and means nothing here; the local
        # path is derived from sha256 (see Source.local_file) once a blob
        # transfer delivers the bytes. An imported source therefore has
        # no file until then, which is truthful.
        incoming_asset = Asset(
            url=data.get("url"),
            sha256=data.get("sha256"),
            filename=data.get("filename"),
            size=data.get("size"),
        )
        source = existing_source or Source(title=data.get("title"))
        source.title = data.get("title")
        source.topics = list(data.get("topics") or [])
        # full_text is not transported - it is re-extracted from the
        # file. Never overwrite a local extraction with nothing.
        if data.get("kind") == "text" and not source.full_text:
            source.full_text = None
        source.asset = incoming_asset
        if existing_source:
            await source.save()
        else:
            await _create_with_id(source, record_id)
        await _apply_timestamp(source, data.get("updated"))
        result["sources_updated" if existing_source else "sources_created"] += 1
        if _needs_blob(source):
            result["sources_awaiting_blobs"] += 1

        slugs = data.get("notebook_slugs") or []
        if not slugs:
            continue
        for slug in slugs:
            nb_id = local_notebooks.get(slug)
            if not nb_id:
                missing.add(slug)
                continue
            if dry_run:
                continue
            try:
                await repo_query(
                    "RELATE $sid->artifact->$nid",
                    {
                        "sid": ensure_record_id(record_id),
                        "nid": ensure_record_id(nb_id),
                    },
                )
            except Exception as exc:  # noqa: BLE001 - one bad edge is not fatal
                logger.warning(f"sync: could not attach source {record_id}: {exc}")

    revlog_result = await _import_revlog(root, dry_run)
    result["revlog_added"] = revlog_result[0]
    result["revlog_duplicates"] = revlog_result[1]
    result["missing_notebooks"] = sorted(missing)
    await _apply_tombstones(root, result, dry_run)
    return result


async def _apply_tombstones(root: Path, result: Dict[str, Any], dry_run: bool) -> None:
    """Delete the records the peer says are gone.

    Resolved by the same last-write-wins rule as everything else: a
    tombstone newer than the local record deletes it, and a local record
    newer than the tombstone ignores it. That second case is what makes
    "edit after a delete brings it back" work, which is the behaviour
    the user asked for - most recent activity wins, whichever device it
    happened on.
    """
    from open_notebook.domain.flashcard import Flashcard
    from open_notebook.domain.notebook import Note

    found = tombstones.load_tombstones(root)
    if not found:
        return

    models = {NOTES_DIR: Note, CARDS_DIR: Flashcard, SOURCES_DIR: Source}

    for record_id, tombstone in found.items():
        model = models.get(tombstone.kind)
        if model is None:
            # Notebooks sync by slug in a single index file and carry no
            # timestamp of their own, so they get no recency comparison.
            result["errors"].append(
                f"tombstone {record_id}: unknown kind {tombstone.kind!r}"
            )
            continue

        existing = await _maybe_get(model, record_id)
        if existing is None:
            result["tombstones_already_absent"] += 1
            continue

        local_updated = (
            existing.updated.isoformat() if existing.updated is not None else None
        )
        if not _newer(tombstone.updated, local_updated):
            # Edited on this device after the peer deleted it. The edit
            # is the newer fact, so the record stays.
            result["tombstones_overridden_by_local_edit"] += 1
            logger.info(
                f"Tombstone for {record_id} ignored: edited locally at "
                f"{local_updated} after the delete at {tombstone.updated}"
            )
            continue

        if dry_run:
            result["tombstones_applied"] += 1
            continue

        # Delete the record, not the bytes. A tombstone means "forget
        # this", not "destroy this": the file may be the last copy on
        # this device, and the peer that issued the delete still has it.
        await existing.delete()
        result["tombstones_applied"] += 1
        logger.info(f"Applied tombstone: deleted {tombstone.kind} {record_id}")


def _blob_present(data: Dict[str, Any]) -> bool:
    """True when this device already holds the bytes a manifest names."""
    return resolve_blob_path(data.get("sha256"), data.get("filename")) is not None


def _needs_blob(source: Source) -> bool:
    """True when a source wants bytes this device does not have.

    Only file sources ever want bytes. A link or a text source is fully
    described by its manifest entry, so counting it as awaiting a blob
    would be a permanent false alarm.
    """
    if not source.asset or not source.asset.sha256:
        return False
    return _source_kind(source.asset) == "file" and not source.has_local_file


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


def _as_iso(value: Any) -> Any:
    """Normalise a timestamp to something a `datetime` field will accept.

    SurrealDB serialises datetimes with a space separator
    (`2026-10-13 06:55:36+00:00`), which is what lands in the exported
    JSONL. Now that migration 34 lets a supplied value through instead of
    overwriting it with `time::now()`, that string is handed straight to
    the field - and `TYPE datetime` rejects it:

        Found '2026-10-13 06:55:36.740943+00:00' for field `reviewed_at`,
        but expected a datetime

    So parse it and re-emit in ISO-8601 with a `T`. Unparseable values
    pass through untouched rather than being dropped, so a malformed row
    surfaces as a schema error naming the value instead of vanishing.
    """
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return value
    return value


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
                "reviewed_at": _as_iso(row.get("reviewed_at")),
                "duration_ms": row.get("duration_ms", 0),
            },
        )
    return added, duplicates


def iter_sync_files(root: Optional[Path] = None) -> Iterable[Path]:
    root = root or sync_dir()
    yield from sorted((root / NOTES_DIR).glob("*.json"))
    yield from sorted((root / CARDS_DIR).glob("*.json"))
