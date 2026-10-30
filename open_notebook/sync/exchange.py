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
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from loguru import logger
from pydantic import BaseModel

from open_notebook.ai.models import SyncConflict, SyncOptOut
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
# Model-generated insights (transformation output per source). Small
# text, stable source ids, worth carrying: regenerating them on the
# phone costs model inference time next to two llama-servers, while
# transporting them costs bytes in git. Embeddings stay local for the
# opposite reason (vectors are model-specific); insights are text.
INSIGHTS_DIR = "insights"
REVLOG = "revlog/revlog.jsonl"
# Chat sessions. One file per session, holding metadata and the
# transcript's hash - never the transcript. That travels as a blob, so
# the repository stays metadata-only and a conversation stays a
# conversation rather than becoming a commit nobody can read.
CHAT_DIR = "chat"


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

    # Opt-out configuration: collections and record ids excluded from sync.
    optout = await _optout()

    notes: List[Note] = await Note.get_all()
    cards: List[Flashcard] = await Flashcard.get_all()
    sources: List[Source] = await Source.get_all()

    # What this device has demonstrably seen, per manifest kind. Both
    # sweeps below are gated on it: files that arrived by fetch without
    # an import are the normal state between two syncs, not deletions,
    # and only ids inside the baseline may be tombstoned. No baseline
    # means never imported, and a device that has never imported
    # tombstones nothing - however old the entries are.
    baseline = tombstones.read_baseline() or {}
    known = {kind: set(ids or []) for kind, ids in baseline.items()}
    known_slugs: Optional[Set[str]] = (
        set(known[NOTEBOOKS]) if NOTEBOOKS in known else None
    )

    # Notebooks are referenced by slug, not id: a record id is per-database
    # and means nothing on the other device. One query each rather than
    # one per note.
    # `updated` is what makes a notebook subject to the same
    # last-write-wins rule as every other record. Without it a deleted
    # notebook could not be distinguished from one that never synced,
    # and there was nothing to compare a tombstone against.
    notebook_rows = await repo_query("SELECT id, name, updated FROM notebook") or []
    notebook_names = {str(r["id"]): r.get("name") or "" for r in notebook_rows}
    notebook_updated = {
        str(r["id"]): r.get("updated") for r in notebook_rows if r.get("updated")
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

    # Slugs are the notebook's identity across devices - a record id is
    # per-database and means nothing on the other side - so the index is
    # keyed by slug and deduped on it.
    notebook_entries: Dict[str, Dict[str, Any]] = {}
    for notebook_id, name in notebook_names.items():
        if _is_opted_out(optout, "notebooks", str(notebook_id)):
            continue
        slug = _slug(name)
        if not slug:
            continue
        stamped = _as_iso(notebook_updated.get(notebook_id))
        notebook_entries[slug] = {
            "slug": slug,
            "name": name,
            "updated": stamped.isoformat() if stamped else None,
        }
    # Swept here, before the index is written, and not alongside the
    # per-record sweep below. Notebooks share one index file, so
    # overwriting it first destroys the only record of what the peer
    # believed existed - there would be nothing left to detect a
    # deletion against. Per-record files are immune to this because each
    # is its own baseline.
    #
    # Gated on the baseline and counted. An earlier version discarded
    # this return value and counted a second sweep placed after the
    # index rewrite instead - which could find nothing by construction,
    # since the fresh index lists exactly what is live. Seven tombstones
    # were written here while the manifest reported `tombstoned: 0`.
    notebook_removed = tombstones.sweep_notebooks(
        root, NOTEBOOKS, notebook_entries.keys(), known_slugs=known_slugs
    )

    _write_json(
        root / NOTEBOOKS,
        {"notebooks": sorted(notebook_entries.values(), key=lambda nb: nb["slug"])},
    )

    for note in notes:
        if _is_opted_out(optout, "notes", str(note.id)):
            continue
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
        if _is_opted_out(optout, "sources", str(source.id)):
            continue
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
                # Provenance. `kind` says whether this is a file or a
                # link; these say *which device* wrote the entry and,
                # when it is a file, which device actually holds the
                # bytes right now.
                #
                # Necessary because the file and the metadata travel by
                # different routes: metadata over git, bytes over the
                # blob channel. So "I have this source" and "I can open
                # this source" are different claims, and only the second
                # one is worth anything. `bytes_held_by` is omitted when
                # the exporting device does not have the file, which is
                # the normal state for a link and the honest state for
                # a file that has not been pushed yet.
                "exported_by": device,
                **(
                    {"bytes_held_by": device}
                    if asset.sha256 and resolve_blob_path(asset.sha256, asset.filename)
                    else {}
                ),
            },
        )

    for card in cards:
        if _is_opted_out(optout, "cards", str(card.id)):
            continue
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

    insights = await _export_insights(root)

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

    chat_entries = await _export_chats(root, device, notebook_names)

    # Deletions are detected here rather than in each delete method.
    # Comparing the manifest against what exists now catches every route
    # that removes a record - the UI endpoint, the notebook cascade, a
    # script - including ones nobody remembered to instrument. Poking
    # the delete methods would cover the ones already known about and
    # miss the next one silently.
    #
    # `notebook_removed` (counted at the pre-index sweep above) joins the
    # per-record sweep here, so the manifest's `tombstoned` count is what
    # was actually written - never a subset of it.
    removed = notebook_removed
    removed += tombstones.sweep(
        root,
        {
            NOTES_DIR: [str(n.id) for n in notes],
            CARDS_DIR: [str(c.id) for c in cards],
            SOURCES_DIR: [str(s.id) for s in sources],
            # Swept like every other kind. A session deleted on this
            # device has to leave the same kind of mark a deleted note
            # does, or the peer keeps offering it back forever and the
            # conversation reappears with no action taken.
            CHAT_DIR: list(chat_entries),
            INSIGHTS_DIR: list(insights),
        },
        known=known,
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
            "chats": len(chat_entries),
            "insights": len(insights),
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
        # Transcripts this device names but does not hold - it heard of
        # them from a peer and the bytes have not come back yet. The
        # inverse of what the sources line says, and the one that tells
        # the channel there is something to push.
        #
        # Keyed on the hash being present *and* unheld, not on the
        # absence of `transcript_exported_by`: an empty conversation has
        # no transcript at all, and counting that as awaiting would
        # report a transfer outstanding for a session with nothing to
        # send.
        "chats_awaiting_blobs": sum(
            1
            for e in chat_entries.values()
            if e.get("transcript_sha256") and not e.get("transcript_exported_by")
        ),
    }
    _write_json(root / MANIFEST, manifest)

    # Record what left this device. A locally-created-then-deleted record
    # must be tombstonable even if it was never imported - the baseline
    # is what this device knows about, from either direction, and an
    # export is knowledge leaving.
    tombstones.write_baseline(
        {
            NOTES_DIR: [str(n.id) for n in notes],
            CARDS_DIR: [str(c.id) for c in cards],
            SOURCES_DIR: [str(s.id) for s in sources],
            CHAT_DIR: list(chat_entries),
            INSIGHTS_DIR: list(insights),
            NOTEBOOKS: sorted(notebook_entries.keys()),
        }
    )
    return manifest


async def _export_insights(root: Path) -> Dict[str, Dict[str, Any]]:
    """Write one file per insight. Returns them by id.

    The `source` link travels as a plain id, not an edge: insights point
    at their source with a record field, and source ids are stable across
    devices (preserved by `_create_with_id`), so there is nothing to
    resolve. A link naming a source this device does not hold is left
    for the import to handle, the same way unresolvable notebook slugs
    are reported rather than hidden.
    """
    (root / INSIGHTS_DIR).mkdir(parents=True, exist_ok=True)

    optout = await _optout()

    rows = (
        await repo_query(
            "SELECT id, insight_type, content, source, updated FROM source_insight"
        )
        or []
    )
    entries: Dict[str, Dict[str, Any]] = {}

    for row in rows:
        insight_id = str(row.get("id") or "")
        if not insight_id:
            continue
        if _is_opted_out(optout, "insights", insight_id):
            continue
        stamped = _as_iso(row.get("updated"))
        entries[insight_id] = {
            "id": insight_id,
            "insight_type": row.get("insight_type"),
            "content": row.get("content"),
            "source_id": str(row.get("source") or "") or None,
            "updated": stamped.isoformat() if stamped else None,
        }
        _write_json(
            root / INSIGHTS_DIR / f"{insight_id.replace(':', '-')}.json",
            entries[insight_id],
        )

    return entries


async def _export_chats(
    root: Path, device: str, notebook_names: Dict[str, str]
) -> Dict[str, Dict[str, Any]]:
    """Write one metadata file per chat session. Returns them by id.

    The transcript is stored as a blob and only its hash is written
    here. `transcript_exported_by` says which device holds the bytes, in
    the same spirit as a source's `bytes_held_by`: "I know about this
    conversation" and "I can read this conversation" are different
    claims, and only the second one is worth anything.

    All messages come back in one query and are bucketed by session,
    rather than asking each session for its own transcript. This started
    by calling `ChatMessage(**row).transcript()` on a *chat_session*
    row, which built a message with no `session` set - so the lookup
    matched nothing, every session exported with no hash, and the entire
    blob path did nothing while looking like it had worked.
    """
    from open_notebook.domain.notebook import ChatMessage
    from open_notebook.sync import chatsync

    (root / CHAT_DIR).mkdir(parents=True, exist_ok=True)

    optout = await _optout()

    rows = (
        await repo_query("SELECT id, title, model_override, updated FROM chat_session")
        or []
    )
    entries: Dict[str, Dict[str, Any]] = {}

    by_session: Dict[str, List[ChatMessage]] = {}
    for raw in (
        await repo_query("SELECT * FROM chat_message ORDER BY seq ASC, created ASC")
        or []
    ):
        message = ChatMessage(**raw)
        by_session.setdefault(str(message.session or ""), []).append(message)

    # What the last export claimed, read before anything is overwritten.
    # A device that has imported a session but not yet its transcript has
    # no messages to serialise - and writing `transcript_sha256: null`
    # would drop the hash on the floor, so the peer would never learn
    # there were bytes to send and the conversation would stay
    # half-synced for good. Pull-then-export is the planned order, not an
    # edge case.
    previous: Dict[str, Dict[str, Any]] = {}
    for existing_file in sorted((root / CHAT_DIR).glob("*.json")):
        data = _read_json(existing_file)
        if data and data.get("id"):
            previous[str(data["id"])] = data

    for row in rows:
        session_id = str(row.get("id") or "")
        if not session_id:
            continue
        if _is_opted_out(optout, "chats", session_id):
            continue

        messages = by_session.get(session_id, [])
        blob_sha: Optional[str] = None
        exported_by: Optional[str] = None
        if messages:
            # Stored unconditionally rather than only when it changed.
            # Content addressing makes a repeat cheap - one hash, no
            # write - and skipping the comparison would mean deciding
            # "changed" against a field that does not exist.
            ref = chatsync.store_transcript(session_id, messages)
            if chatsync.blob_exists(ref.sha256):
                blob_sha, exported_by = ref.sha256, device
        else:
            # No messages here, so nothing to store. Carry the hash the
            # peer published instead, and deliberately do *not* claim to
            # hold it: that is what tells the other device to push.
            carried = previous.get(session_id, {}).get("transcript_sha256")
            if carried:
                blob_sha = str(carried)

        notebook_slugs: set[str] = set()
        for edge in (
            await repo_query(
                "SELECT out FROM refers_to WHERE in = $sid",  # noqa: S608
                {"sid": ensure_record_id(session_id)},
            )
            or []
        ):
            slug = _slug(notebook_names.get(str(edge.get("out")), ""))
            if slug:
                notebook_slugs.add(slug)

        stamped = _as_iso(row.get("updated"))
        entries[session_id] = {
            "id": session_id,
            "title": row.get("title"),
            "model_override": row.get("model_override"),
            "updated": stamped.isoformat() if stamped else None,
            "message_count": len(messages),
            "notebook_slugs": sorted(notebook_slugs),
            "transcript_sha256": blob_sha,
            # Absent, not null, when this device does not hold the
            # bytes - the same convention sources use.
            **({"transcript_exported_by": exported_by} if exported_by else {}),
            "exported_by": device,
        }
        _write_json(
            root / CHAT_DIR / f"{session_id.replace(':', '-')}.json",
            entries[session_id],
        )

    return entries


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


async def _optout() -> SyncOptOut:
    """Load the sync opt-out configuration (singleton)."""
    return await SyncOptOut.get_instance()


def _is_opted_out(optout: SyncOptOut, collection: str, record_id: str) -> bool:
    """True if this collection or record id is excluded from sync."""
    if collection in optout.collections:
        return True
    if record_id in optout.record_ids:
        return True
    return False


def _records_differ(local: Optional[Dict[str, Any]], remote: Optional[Dict[str, Any]]) -> bool:
    """True if two record dicts differ in any field (full-record comparison)."""
    if local is None and remote is None:
        return False
    if local is None or remote is None:
        return True
    # Normalise: compare sorted JSON serialization
    import json
    return json.dumps(local, sort_keys=True, default=str) != json.dumps(remote, sort_keys=True, default=str)


async def _queue_conflict(
    collection: str,
    db_record_id: str,
    local_updated: Optional[str],
    remote_updated: Optional[str],
    local_data: Optional[Dict[str, Any]],
    remote_data: Optional[Dict[str, Any]],
) -> None:
    """Record a sync conflict for user resolution."""
    conflict = await SyncConflict.get_instance()
    if conflict.resolved:
        # A previous conflict was already resolved; clear it first.
        conflict.resolved = False
        conflict.resolution = None
        conflict.resolved_at = None
    conflict.collection = collection
    conflict.db_record_id = db_record_id
    conflict.local_updated = local_updated
    conflict.remote_updated = remote_updated
    conflict.local_data = local_data or {}
    conflict.remote_data = remote_data or {}
    conflict.resolved = False
    conflict.resolution = None
    conflict.resolved_at = None
    await conflict.update()


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
    missing: set[str] = set()
    optout = await _optout()

    # Notebooks first. Everything downstream resolves a `notebook_slugs`
    # entry to a local notebook id, so this has to run before the notes
    # and sources are processed.
    #
    # This step did not exist at all before: the index was exported and
    # then never read, so a notebook that only ever existed on the PC
    # simply never appeared on the phone. Anything whose slug had no
    # local match was dropped into `missing_notebooks` and left there.
    # What this device has seen. Records absent locally but present here
    # were deleted on this device and not yet published as tombstones:
    # creating them from the manifest would resurrect the device's own
    # deletions on every import, and the tombstone could never be born.
    # Five seeds of randomised two-device interleavings converged on
    # nothing until this gate existed - deletions died the cycle they
    # were made in, re-imported by the deleter before it ever exported.
    seen = tombstones.read_baseline() or {}
    (
        notebooks_created,
        notebooks_updated,
        notebooks_resurrected,
    ) = await _import_notebooks(root, dry_run, set(seen.get(NOTEBOOKS, ())))

    # Rebuilt after the import above, not before it. The map used to be
    # snapshotted here from the pre-import database, so on a device
    # receiving new notebooks every slug missed: the notebooks were
    # created and then nothing was ever filed into them. Seven notebooks
    # arrived on the phone with all of their sources left unattached,
    # and the import reported success.
    async def _notebook_index() -> Dict[str, str]:
        return {
            _slug(r.get("name") or ""): str(r["id"])
            for r in (await repo_query("SELECT id, name FROM notebook") or [])
        }

    local_notebooks = await _notebook_index()

    # Sessions next, once notebooks exist - a session's notebook_slugs
    # resolve against the same index the notes and sources use.
    chats = await _import_chats(
        root, dry_run, local_notebooks, missing, set(seen.get(CHAT_DIR, ()))
    )

    result: Dict[str, Any] = {
        "notebooks_created": 0,
        "notebooks_updated": 0,
        # Records the manifest names that this device deleted and has
        # not yet published as tombstones. Import skips recreating them
        # (which would resurrect the deletion before the tombstone is
        # ever exported); the next export publishes the tombstone and
        # the peer deletes too.
        "resurrections_suppressed": 0,
        "notes_created": 0,
        "notes_updated": 0,
        "notes_skipped_stale": 0,
        "cards_created": 0,
        "cards_updated": 0,
        "cards_skipped_stale": 0,
        "sources_created": 0,
        "sources_updated": 0,
        "sources_skipped_stale": 0,
        "insights_created": 0,
        "insights_updated": 0,
        "insights_skipped_stale": 0,
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
        # Chat sessions, and the two numbers that are not the same
        # thing: `messages_added` counts messages that actually arrived,
        # `chats_awaiting_blobs` counts sessions whose transcript is
        # named in the manifest but not yet on this device.
        "chats_created": chats["created"],
        "chats_updated": chats["updated"],
        "chats_skipped_stale": chats["skipped_stale"],
        "chats_messages_added": chats["messages_added"],
        "chats_awaiting_blobs": chats["awaiting_blobs"],
        # Deletions carried by the peer. `overridden_by_local_edit` is
        # the record coming back because this device edited it after the
        # delete - correct, and worth seeing rather than hiding.
        "tombstones_applied": 0,
        "tombstones_overridden_by_local_edit": 0,
        "tombstones_already_absent": 0,
        "errors": [],
        "dry_run": dry_run,
    }
    result["resurrections_suppressed"] += notebooks_resurrected + chats["resurrected"]

    for path in sorted((root / NOTES_DIR).glob("*.json")):
        data = _read_json(path)
        if not data or not data.get("id"):
            result["errors"].append(f"{path.name}: no id")
            continue
        record_id = data["id"]
        existing_note: Optional[Note] = await _maybe_get(Note, record_id)
        if existing_note is None and record_id in seen.get(NOTES_DIR, ()):
            logger.debug(f"sync: not recreating locally deleted note {record_id}")
            result["resurrections_suppressed"] += 1
            continue

        local_updated = (
            existing_note.updated.isoformat()
            if (existing_note and existing_note.updated)
            else None
        )
        # Opt-out: never import records the operator wants device-local.
        if _is_opted_out(optout, "notes", record_id):
            continue
        # Staleness gates the field upsert, not the filing: a record
        # skipped as stale still converges its notebook membership
        # below, which is what lets a repeated import repair a missed
        # attachment instead of skipping past it again.
        note_stale = bool(
            existing_note and not _newer(data.get("updated"), local_updated)
        )
        # Full-record conflict detection: same updated but different
        # content is not a conflict; different updated AND different
        # content means both sides edited and the user must choose.
        if existing_note and not note_stale and data.get("updated") != local_updated:
            note_local_data: Dict[str, Any] = {
                "title": existing_note.title,
                "content": existing_note.content,
                "note_type": existing_note.note_type,
                "updated": local_updated,
            }
            if _records_differ(note_local_data, data):
                await _queue_conflict(
                    "notes",
                    record_id,
                    local_updated,
                    data.get("updated"),
                    note_local_data,
                    data,
                )
                result["notes_skipped_stale"] += 1
                continue
        if note_stale:
            result["notes_skipped_stale"] += 1
        elif dry_run:
            result["notes_created" if not existing_note else "notes_updated"] += 1
        else:
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

        if not dry_run:
            if await _attach_slugs(
                record_id,
                list(data.get("notebook_slugs") or []),
                "artifact",
                local_notebooks,
                missing,
                dry_run,
            ):
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
        if existing_card is None and record_id in seen.get(CARDS_DIR, ()):
            logger.debug(f"sync: not recreating locally deleted card {record_id}")
            result["resurrections_suppressed"] += 1
            continue

        local_updated = (
            existing_card.updated.isoformat()
            if (existing_card and existing_card.updated)
            else None
        )
        # Opt-out: never import records the operator wants device-local.
        if _is_opted_out(optout, "cards", record_id):
            continue
        # Full-record conflict detection: different updated AND different
        # content means both sides edited and the user must choose.
        if existing_card and data.get("updated") != local_updated:
            card_local_data: Dict[str, Any] = {
                "model": existing_card.model,
                "front": existing_card.front,
                "back": existing_card.back,
                "cloze_text": existing_card.cloze_text,
                "tags": existing_card.tags,
                "guid": existing_card.guid,
                "status": existing_card.status,
                "fsrs_json": existing_card.fsrs_json,
                "updated": local_updated,
            }
            if _records_differ(card_local_data, data):
                await _queue_conflict(
                    "cards",
                    record_id,
                    local_updated,
                    data.get("updated"),
                    card_local_data,
                    data,
                )
                result["cards_skipped_stale"] += 1
                continue
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
        if existing_source is None and record_id in seen.get(SOURCES_DIR, ()):
            logger.debug(f"sync: not recreating locally deleted source {record_id}")
            result["resurrections_suppressed"] += 1
            continue

        local_updated = (
            existing_source.updated.isoformat()
            if (existing_source and existing_source.updated)
            else None
        )
        # Opt-out: never import records the operator wants device-local.
        if _is_opted_out(optout, "sources", record_id):
            continue
        # Full-record conflict detection: different updated AND different
        # content means both sides edited and the user must choose.
        if existing_source and data.get("updated") != local_updated:
            source_local_data: Dict[str, Any] = {
                "title": existing_source.title,
                "topics": existing_source.topics,
                "updated": local_updated,
                "asset": {
                    "url": existing_source.asset.url if existing_source.asset else None,
                    "sha256": existing_source.asset.sha256 if existing_source.asset else None,
                    "filename": existing_source.asset.filename if existing_source.asset else None,
                    "size": existing_source.asset.size if existing_source.asset else None,
                },
            }
            if _records_differ(source_local_data, data):
                await _queue_conflict(
                    "sources",
                    record_id,
                    local_updated,
                    data.get("updated"),
                    source_local_data,
                    data,
                )
                result["sources_skipped_stale"] += 1
                continue
        # As with notes: staleness gates the field upsert, and an
        # outstanding blob is still counted on the stale path, but the
        # filing below runs either way.
        source_stale = bool(
            existing_source and not _newer(data.get("updated"), local_updated)
        )
        if source_stale:
            result["sources_skipped_stale"] += 1
            # Still count an outstanding blob: staleness is about the
            # metadata, not about whether this device can open the file.
            if existing_source and _needs_blob(existing_source):
                result["sources_awaiting_blobs"] += 1
        elif dry_run:
            result["sources_created" if not existing_source else "sources_updated"] += 1
            if data.get("sha256") and not _blob_present(data):
                result["sources_awaiting_blobs"] += 1
            continue
        else:
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

        if not dry_run:
            # Filed with `reference`, the table the UI writes and reads.
            # An earlier version wrote `artifact` edges here, which the
            # export tolerates but `get_sources` never looks at - so the
            # import reported success while the notebook view stayed
            # empty.
            await _attach_slugs(
                record_id,
                list(data.get("notebook_slugs") or []),
                "reference",
                local_notebooks,
                missing,
                dry_run,
            )

    insights_result = await _import_insights(root, dry_run, seen)
    result["insights_created"] = insights_result[0]
    result["insights_updated"] = insights_result[1]
    result["insights_skipped_stale"] = insights_result[2]
    result["resurrections_suppressed"] += insights_result[3]

    revlog_result = await _import_revlog(root, dry_run)
    result["revlog_added"] = revlog_result[0]
    result["revlog_duplicates"] = revlog_result[1]
    result["missing_notebooks"] = sorted(missing)
    result["notebooks_created"] = notebooks_created
    result["notebooks_updated"] = notebooks_updated
    await _apply_tombstones(root, result, dry_run, local_notebooks)

    # Only a completed import arms the export sweep. Until this runs,
    # nothing on this device may be tombstoned: with no baseline the
    # sweeps have nothing they are allowed to forget. The baseline
    # records what arrived, keyed the same way the sweeps look things
    # up - record ids for files, slugs for the shared notebook index.
    if not dry_run:
        tombstones.write_watermark()
        tombstones.write_baseline(_imported_baseline(root))

        # Sources completed by an earlier blob transfer get their text
        # extracted now, the way an upload would have. Without this a
        # synced library reports every hash resolved while every file
        # opens as "No content available" - the bytes arriving is not
        # the same as the text existing.
        #
        # Guarded, not merely called: queuing is an enhancement to the
        # import, and an enhancement must never break the thing it
        # follows. If the scan fails, the records are still imported and
        # the next import - or the manual /blobs/process endpoint - tries
        # again.
        try:
            from open_notebook.sync.extraction import enqueue_missing_extraction

            extraction = await enqueue_missing_extraction()
            result["sources_queued_for_extraction"] = extraction["queued_count"]
            # Surfaced, not swallowed: without these ids a skipped file
            # is invisible, and three turns of diagnosis proved that an
            # invisible skip reads as a broken queue.
            result["extraction_needs_retry"] = extraction["needs_retry"]
        except Exception as exc:  # noqa: BLE001 - queuing never breaks importing
            logger.warning(f"extraction queueing failed after import: {exc}")
            result["sources_queued_for_extraction"] = 0

    return result


async def _import_chats(
    root: Path,
    dry_run: bool,
    local_notebooks: Dict[str, str],
    missing: set,
    seen_ids: set | None = None,
) -> Dict[str, int]:
    """Upsert chat sessions, and merge transcripts whose bytes are here.

    A session whose transcript has not arrived still imports. Reporting
    it as `awaiting_blobs` rather than failing is the point: metadata
    and bytes travel by different routes, in that order, so "the manifest
    knows about bytes I have not fetched" is the normal state of a device
    between two syncs rather than an error to shout about.

    Sessions are filed with `refers_to` edges like everything else. The
    manifest carried `notebook_slugs` for sessions from the start, and
    the import ignored them - so synced conversations arrived unfiled,
    visible nowhere except a flat session list.
    """
    from open_notebook.domain.notebook import ChatSession
    from open_notebook.sync import chatsync

    stats = {
        "created": 0,
        "updated": 0,
        "skipped_stale": 0,
        "resurrected": 0,
        "messages_added": 0,
        "awaiting_blobs": 0,
    }
    chat_dir = root / CHAT_DIR
    if not chat_dir.is_dir():
        return stats

    for path in sorted(chat_dir.glob("*.json")):
        data = _read_json(path)
        if not data or not data.get("id"):
            continue

        session_id = str(data["id"])
        existing = await _maybe_get(ChatSession, session_id)
        if existing is None and seen_ids is not None and session_id in seen_ids:
            logger.debug(f"sync: not recreating locally deleted session {session_id}")
            stats["resurrected"] += 1
            continue
        is_stale = existing is not None and not _newer(
            data.get("updated"), _iso_str(existing.updated)
        )

        if is_stale:
            stats["skipped_stale"] += 1
        elif not dry_run:
            if existing is None:
                await _create_with_id(
                    ChatSession(
                        title=data.get("title"),
                        model_override=data.get("model_override"),
                    ),
                    session_id,
                )
                stats["created"] += 1
            else:
                existing.title = data.get("title")
                existing.model_override = data.get("model_override")
                await existing.update()
                stats["updated"] += 1
            await _apply_timestamp(
                await _maybe_get(ChatSession, session_id) or ChatSession(),
                data.get("updated"),
            )

        # Filing, like the transcript below, runs for stale sessions
        # too: membership is not record content, and a repeated import
        # repairs a missed attachment instead of skipping past it.
        if not dry_run:
            await _attach_slugs(
                session_id,
                list(data.get("notebook_slugs") or []),
                "refers_to",
                local_notebooks,
                missing,
                dry_run,
            )

        # Deliberately outside the branch above. Whether the metadata is
        # stale says nothing about whether the bytes have arrived: the
        # normal order is metadata first and blobs on a later pass, so a
        # session whose title has not changed since the last import is
        # exactly the one whose transcript is still missing. Skipping
        # the transcript on a stale comparison left those permanently
        # half-synced, with the blob sitting in the store and nothing
        # ever reading it.
        sha = data.get("transcript_sha256")
        if not sha:
            continue
        if chatsync.transcript_pending(sha):
            stats["awaiting_blobs"] += 1
            continue
        if not dry_run:
            added, _seen = await chatsync.import_transcript(session_id, str(sha))
            stats["messages_added"] += added

    return stats


async def _attach_slugs(
    record_id: str,
    slugs: List[str],
    edge_table: str,
    local_notebooks: Dict[str, str],
    missing: set,
    dry_run: bool,
) -> bool:
    """File a record into notebooks by slug. Returns whether it is filed.

    Idempotent: edges already present are skipped, because RELATE always
    inserts and a second import would otherwise double every membership.
    Staleness is irrelevant here - membership is not record content, so
    a record skipped as stale still converges its filing. That is what
    makes a repeated import repair a missed one rather than repeating it.

    The edge table is the same one the UI writes, or the filing is
    invisible: notes attach with `artifact`, sources with `reference`,
    sessions with `refers_to`. Writing a source as an `artifact` edge
    files it where `get_sources` never looks - attached by every measure
    except the one the user sees.
    """
    if not slugs:
        return False
    linked = (
        await repo_query(
            f"SELECT VALUE out FROM {edge_table} WHERE in = $rid",  # noqa: S608
            {"rid": ensure_record_id(record_id)},
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
            await repo_query(
                f"RELATE $in->{edge_table}->$out",  # noqa: S608
                {
                    "in": ensure_record_id(record_id),
                    "out": ensure_record_id(nb_id),
                },
            )
            attached = True
        except Exception as exc:  # noqa: BLE001 - one bad edge is not fatal
            logger.warning(f"sync: could not attach {record_id}: {exc}")
    return attached


def _iso_str(value: Any) -> Optional[str]:
    return value.isoformat() if value is not None else None


def _imported_baseline(root: Path) -> Dict[str, List[str]]:
    """The ids the manifest currently names, for the baseline.

    Read from the files just imported, not from the database: the point
    is recording what this device has *seen*, and the files are the
    record of that. Keyed exactly as the sweeps look things up.
    """
    snapshot: Dict[str, List[str]] = {}
    for directory in (NOTES_DIR, CARDS_DIR, SOURCES_DIR, CHAT_DIR, INSIGHTS_DIR):
        folder = root / directory
        ids: List[str] = []
        if folder.is_dir():
            for path in sorted(folder.glob("*.json")):
                data = _read_json(path)
                if data and data.get("id"):
                    ids.append(str(data["id"]))
        snapshot[directory] = ids

    slugs: List[str] = []
    index = _read_json(root / NOTEBOOKS)
    if index:
        for entry in index.get("notebooks") or []:
            if entry.get("slug"):
                slugs.append(str(entry["slug"]))
    snapshot[NOTEBOOKS] = slugs
    return snapshot


async def _import_insights(
    root: Path, dry_run: bool, seen: Dict[str, list]
) -> Tuple[int, int, int, int]:
    """Upsert insights by record id. Returns (created, updated, stale, resurrected).

    Runs after sources so the linked records normally exist, but does
    not require them: the link is a plain record field and SurrealDB
    does not enforce it, so an insight whose source has not arrived yet
    still imports and resolves when it does. Deleting the insight when
    its source is missing would destroy synced content over ordering.
    """
    from open_notebook.domain.notebook import SourceInsight

    created = updated = stale = resurrected = 0
    insights_dir = root / INSIGHTS_DIR
    if not insights_dir.is_dir():
        return created, updated, stale, resurrected
    known_ids = set(seen.get(INSIGHTS_DIR, ()))

    for path in sorted(insights_dir.glob("*.json")):
        data = _read_json(path)
        if not data or not data.get("id"):
            continue
        record_id = str(data["id"])
        existing = await _maybe_get(SourceInsight, record_id)
        if existing is None and record_id in known_ids:
            logger.debug(f"sync: not recreating locally deleted insight {record_id}")
            resurrected += 1
            continue

        local_updated = (
            existing.updated.isoformat() if (existing and existing.updated) else None
        )
        if existing is not None and not _newer(data.get("updated"), local_updated):
            stale += 1
            continue
        if dry_run:
            if existing is not None:
                updated += 1
            else:
                created += 1
            continue

        if existing is None:
            # Raw CREATE, the way the create_insight command writes it:
            # the constructor drops the `source` link (undeclared
            # field), and creating without it fails validation - so a
            # two-step create-then-link cannot exist. The link is part
            # of the record from the first write.
            source_link = data.get("source_id")
            try:
                await repo_query(
                    """
                    CREATE $id CONTENT {
                        insight_type: $kind, content: $content,
                        source: $src, updated: time::now()
                    }
                    """,
                    {
                        "id": ensure_record_id(record_id),
                        "kind": data.get("insight_type") or "Insight",
                        "content": data.get("content") or "",
                        "src": (
                            ensure_record_id(str(source_link)) if source_link else None
                        ),
                    },
                )
            except Exception as exc:
                # Logged, not counted anywhere: the manifest file
                # persists, so the next import retries. (Appending to
                # the import result is impossible here - this helper
                # returns counts, and the caller owns the result.)
                logger.warning(f"sync: could not create insight {record_id}: {exc}")
                continue
            existing = await _maybe_get(SourceInsight, record_id)
            created += 1
        else:
            existing.insight_type = data.get("insight_type") or existing.insight_type
            existing.content = data.get("content") or ""
            await existing.save()
            updated += 1
            source_link = data.get("source_id")
            if source_link:
                try:
                    await repo_query(
                        "UPDATE $id SET source = $src",
                        {
                            "id": ensure_record_id(record_id),
                            "src": ensure_record_id(str(source_link)),
                        },
                    )
                except Exception as exc:  # noqa: BLE001 - link failure is not fatal
                    logger.warning(f"sync: could not link insight {record_id}: {exc}")
        if existing is not None:
            await _apply_timestamp(existing, data.get("updated"))

    return created, updated, stale, resurrected


async def _import_notebooks(
    root: Path, dry_run: bool, seen_slugs: set | None = None
) -> Tuple[int, int, int]:
    """Create or rename notebooks to match the manifest.

    Matched on slug, which is the only identity two devices share - a
    record id is per-database. A notebook present locally under a
    different name is renamed only when the manifest is newer, so the
    same last-write-wins rule applies here as to notes and sources.

    A slug seen before but absent now was deleted here and not yet
    published: recreating it would resurrect the deletion before the
    tombstone is ever exported. Returns (created, updated, resurrected).
    """
    from open_notebook.domain.notebook import Notebook

    path = root / NOTEBOOKS
    if not path.is_file():
        return 0, 0, 0

    try:
        entries = json.loads(path.read_text(encoding="utf-8")).get("notebooks") or []
    except (OSError, ValueError) as exc:
        logger.warning(f"sync: unreadable notebook index: {exc}")
        return 0, 0, 0

    existing = {
        _slug(r.get("name") or ""): r
        for r in (await repo_query("SELECT id, name, updated FROM notebook") or [])
    }

    created = updated = resurrected = 0
    for entry in entries:
        slug = str(entry.get("slug") or "")
        name = entry.get("name") or slug
        if not slug:
            continue

        match = existing.get(slug)
        if match is None and seen_slugs is not None and slug in seen_slugs:
            logger.debug(f"sync: not recreating locally deleted notebook {slug}")
            resurrected += 1
            continue
        if match is None:
            if dry_run:
                created += 1
                continue
            notebook = Notebook(name=name, description="")
            await notebook.save()
            await _apply_timestamp(notebook, entry.get("updated"))
            created += 1
            existing[slug] = {"id": str(notebook.id), "name": name}
            continue

        incoming = entry.get("updated")
        local_updated = match.get("updated")
        local_iso = _as_iso(local_updated)
        local_iso = (
            local_iso.isoformat() if isinstance(local_iso, datetime) else local_iso
        )
        if incoming and not _newer(incoming, local_iso):
            continue
        if dry_run:
            updated += 1
            continue

        notebook = await Notebook.get(str(match["id"]))
        if (notebook.name or "") != name:
            notebook.name = name
            await notebook.save()
        await _apply_timestamp(notebook, incoming)
        updated += 1

    return created, updated, resurrected


async def _apply_notebook_tombstone(
    tombstone: "tombstones.Tombstone",
    result: Dict[str, Any],
    dry_run: bool,
    local_notebooks: Dict[str, str],
) -> None:
    """Forget a notebook the peer says is gone, matched by slug.

    Same rule as everything else, keyed on the slug rather than a record
    id because the id is per-database and means nothing on the other
    device. The notebook record goes, but its members only become
    unfiled: notes, sessions and sources survive with their edges
    removed, and a later export carries them as unattached rather than
    deleting them anywhere.

    Unlinking rather than cascading is the whole point. This used to
    call `notebook.delete()`, whose cascade destroyed member notes and
    referring sessions - 7 notebook tombstones applied on the PC deleted
    2 notes and 4 sessions with them, while the phone still held the
    notes under the same ids. Same records, dead on one side and alive
    on the other, is the worst possible outcome: a divergent destruction
    that last-write-wins cannot reconcile, because neither side knows
    the other disagrees. A folder being forgotten must never destroy its
    contents.
    """
    from open_notebook.domain.notebook import Notebook

    slug = tombstone.id.split(":", 1)[-1]
    notebook_id = local_notebooks.get(slug)
    if notebook_id is None:
        result["tombstones_already_absent"] += 1
        return

    notebook = await Notebook.get(notebook_id)
    local_updated = (
        notebook.updated.isoformat() if notebook.updated is not None else None
    )
    if not _newer(tombstone.updated, local_updated):
        result["tombstones_overridden_by_local_edit"] += 1
        logger.info(
            f"Notebook tombstone for {slug} ignored: edited locally at "
            f"{local_updated} after the delete at {tombstone.updated}"
        )
        return

    if dry_run:
        result["tombstones_applied"] += 1
        return

    # Unlink every member edge, then delete the bare record with
    # repo_delete - deliberately not `notebook.delete()`, whose cascade
    # destroys member notes and referring sessions. A source, note or
    # session that merely hung off this notebook still exists; it just
    # stops being filed here. Deleting the user's material because they
    # tidied a folder is the failure this is written against.
    notebook_record = ensure_record_id(notebook_id)
    for edge_table in ("artifact", "reference", "refers_to"):
        await repo_query(
            f"DELETE {edge_table} WHERE out = $nid",  # noqa: S608
            {"nid": notebook_record},
        )
    from open_notebook.database.repository import repo_delete

    await repo_delete(notebook_id)
    result["tombstones_applied"] += 1
    logger.info(f"Applied notebook tombstone: forgot notebook {slug}, members unfiled")


async def _apply_tombstones(
    root: Path,
    result: Dict[str, Any],
    dry_run: bool,
    local_notebooks: Optional[Dict[str, str]] = None,
) -> None:
    """Delete the records the peer says are gone.

    Resolved by the same last-write-wins rule as everything else: a
    tombstone newer than the local record deletes it, and a local record
    newer than the tombstone ignores it. That second case is what makes
    "edit after a delete brings it back" work, which is the behaviour
    the user asked for - most recent activity wins, whichever device it
    happened on.
    """
    from open_notebook.domain.flashcard import Flashcard
    from open_notebook.domain.notebook import ChatSession, Note, SourceInsight

    found = tombstones.load_tombstones(root)
    if not found:
        return

    models = {
        NOTES_DIR: Note,
        CARDS_DIR: Flashcard,
        SOURCES_DIR: Source,
        # Sessions are tombstoned like any other record, so deleting a
        # conversation on one device stops it being resurrected by the
        # other's manifest on the next export.
        CHAT_DIR: ChatSession,
        INSIGHTS_DIR: SourceInsight,
    }

    for record_id, tombstone in found.items():
        if tombstone.kind == tombstones.NOTEBOOKS_KIND:
            await _apply_notebook_tombstone(
                tombstone, result, dry_run, local_notebooks or {}
            )
            continue

        model = models.get(tombstone.kind)
        if model is None:
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

        if tombstone.kind == CHAT_DIR:
            await _delete_session_and_transcript(existing)

        # Delete the record, not the bytes, and say so explicitly rather
        # than inheriting the local auto_delete_files preference. A
        # tombstone is someone else's delete arriving here; this device
        # may hold the only copy of the file, and destroying it would turn
        # a tidy-up on the phone into permanent loss on the PC.
        await _delete_preserving_blob(existing)
        result["tombstones_applied"] += 1
        logger.info(f"Applied tombstone: deleted {tombstone.kind} {record_id}")


async def _delete_preserving_blob(record: Any) -> None:
    """Delete *record*, asking it to keep its bytes if it has any.

    Only `Source` has bytes, and only `Source.delete` accepts the flag.
    Passing it unconditionally raised `TypeError` on every other kind -
    note, flashcard, chat session - because they inherit
    `ObjectModel.delete(self)`. That was reachable and untested: delete a
    note on the phone and the PC's import died part-way through
    applying the rest of the tombstones.

    Checked rather than caught. A try/except around the call would
    swallow a TypeError thrown from *inside* a delete for the wrong
    reason and leave the record half-removed; inspecting the signature
    only ever passes the argument to a delete that asked for it.
    """
    import inspect

    try:
        accepts = "keep_blob" in inspect.signature(record.delete).parameters
    except (TypeError, ValueError):
        accepts = False
    if accepts:
        await record.delete(keep_blob=True)
    else:
        await record.delete()


async def _delete_session_and_transcript(session: Any) -> None:
    """Remove a chat session's messages before the session itself.

    `chat_message.session` is a plain `record<chat_session>` with no
    ON DELETE CASCADE, so deleting the session leaves its rows behind.
    They are invisible - every read is scoped by session - but not
    harmless: a session id re-imported later adopts the orphans, and a
    conversation the user deleted comes back with the parts they had
    removed.
    """
    session_id = str(session.id)
    try:
        await repo_query(
            "DELETE chat_message WHERE session = $sid",
            {"sid": ensure_record_id(session_id)},
        )
    except Exception as exc:
        # Reported, not raised. The session still has to go; leaving it
        # because its messages could not be cleaned up would be a worse
        # outcome than some rows surviving.
        logger.warning(f"could not clear transcript of {session_id}: {exc}")


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


def load_provenance(
    root: Optional[Path] = None,
) -> Dict[str, Dict[str, Any]]:
    """Which device last wrote each source entry, and who holds its bytes.

    Read from the manifest rather than the database, because provenance
    is a property of the *sync*, not of the record: two devices can hold
    the same record and disagree about who has the file, and only the
    manifest knows which one exported last.

    Returns {} when the sync directory does not exist yet, so a fresh
    install is not an error.
    """
    base = root or sync_dir()
    directory = base / SOURCES_DIR
    if not directory.is_dir():
        return {}

    found: Dict[str, Dict[str, Any]] = {}
    for path in sorted(directory.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        record_id = data.get("id")
        if not record_id:
            continue
        entry: Dict[str, Any] = {
            "origin": data.get("kind"),
            "exported_by": data.get("exported_by"),
            "bytes_held_by": data.get("bytes_held_by"),
        }
        # Preserve an older entry's holder when the newest export did not
        # claim one. Omission means "this device does not have it", not
        # "nobody has it" - the file may well be sitting on the peer.
        previous = found.get(str(record_id))
        if previous and previous.get("bytes_held_by") and not entry["bytes_held_by"]:
            entry["bytes_held_by"] = previous["bytes_held_by"]
        found[str(record_id)] = entry
    return found
