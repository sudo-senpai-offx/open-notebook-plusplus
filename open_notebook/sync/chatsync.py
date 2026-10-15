"""Carry a chat conversation across devices without putting it in git.

What travels, and what does not
-------------------------------
The sync repository holds **metadata and hashes only**. A transcript is
therefore not written there. It is serialised, stored through the same
content-addressed blob store source files use, and travels over the blob
channel; the manifest records only its `sha256`.

That is the same rule sources already follow, and for the same reason: a
study conversation is course content. A repository that accumulates one
per session stops being metadata and becomes a library, which is exactly
what the repo is not for.

One blob per session
--------------------
Not one per message. The alternatives were weighed:

  * per message - each is tiny, so nothing large is ever transferred.
    But a fifty-message conversation becomes fifty blobs, fifty rows in
    the manifest's awaiting-list, and fifty requests. The conversation
    is the unit a person thinks in, and it is the unit that arrives
    whole or not at all.

  * per session - one blob, one hash, one transfer. A session grows by
    rewriting one blob rather than appending fifty, which is fine:
    content addressing makes the old one unreferenced, and unreferenced
    is recoverable.

The cost is size, so there is a threshold. `TRANSCRIPT_WARN_BYTES` is a
warning and not an error: a conversation long enough to trip it is
exactly the one worth having, and refusing to sync it would be the app
deciding that someone's study history is too big to keep.

Why message ids are the merge key
---------------------------------
Migration 36 already made `chat_message.id` unique and intended it for
this. An append-only union on id means both devices can add to one
conversation with no conflict: a message is either already there (skip)
or new (insert). Two devices that each typed into the same session
interleave, and the one that loses the race loses nothing, because the
loser's message is simply already present.

Ordering is deliberately *not* the merge key. Two devices assign
overlapping `seq` values and that is expected; display order falls back
to `created`, then to id.

A missing blob is pending, not a failure
----------------------------------------
Metadata and bytes travel by different routes, in that order. So on any
given sync the manifest will regularly name a transcript whose bytes
have not arrived yet. That is a normal state, not an error: the session
imports as metadata, and a later import picks up the transcript once the
blob is on disk. Failing the import instead would make the two routes
look like one.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from loguru import logger

from open_notebook.storage.blobs import (
    BlobRef,
    blob_exists,
    resolve_blob_path,
    store_bytes,
)

# Bumped if the payload shape changes incompatibly. The reader refuses a
# version it does not know rather than guessing at a layout.
TRANSCRIPT_FORMAT = "open-notebook-chat-transcript/1"

# Above this a single blob is worth mentioning. It is not a limit.
# Sized against the phone's memory rather than its disk: the payload is
# held in full while it is hashed, written and sent, so a blob large
# enough to matter is one that can spike a constrained process.
TRANSCRIPT_WARN_BYTES = 256 * 1024


class TranscriptError(ValueError):
    """Raised when a transcript blob cannot be understood."""


def serialize_transcript(session_id: str, messages: Sequence[Any]) -> bytes:
    """Render a session's messages into the wire format.

    Message record ids are carried, not regenerated. They are the merge
    key, and an id minted here instead of transported would make every
    sync a fresh set of duplicates.

    Messages are sorted by (seq, id) before serialising, so the hash
    depends on the conversation's *content* and not on the order the
    rows happened to arrive in. Two devices holding the same messages
    must produce the same blob, or each pushes its own copy of every
    conversation forever. `import_transcript` merges by id and takes
    seq from the payload, so nothing downstream depends on the order
    they were written in - which is exactly why it is safe to impose one.
    """
    rows: List[Dict[str, Any]] = []
    for message in messages or []:
        content = _field(message, "content", "")
        rows.append(
            {
                "id": str(_field(message, "id", "") or ""),
                "seq": int(_field(message, "seq", 0) or 0),
                "role": str(_field(message, "role", "") or "human"),
                "content": content if isinstance(content, str) else str(content),
                "created": _iso(_field(message, "created", None)),
                # Transported so both sides agree on when the message
                # happened. Without it the importer stamps its own now(),
                # and two copies of one message permanently disagree by
                # the delay between them - which reads as divergence to
                # anything comparing records.
                "updated": _iso(_field(message, "updated", None)),
            }
        )

    rows.sort(key=lambda row: (row["seq"], row["id"]))

    payload = {
        "format": TRANSCRIPT_FORMAT,
        "session_id": str(session_id),
        "messages": rows,
    }
    # Sorted keys for the same reason, one level down: key order alone
    # would otherwise make two identical transcripts hash differently.
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def parse_transcript(data: bytes) -> Dict[str, Any]:
    """Decode a transcript blob, refusing anything we cannot read.

    A wrong-version or corrupt blob raises rather than returning empty.
    An empty parse would look identical to "this conversation has no
    messages", and the difference is the whole content of the thing.
    """
    try:
        payload = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TranscriptError(f"transcript is not readable JSON: {exc}") from exc

    if not isinstance(payload, dict):
        raise TranscriptError(
            f"transcript is a {type(payload).__name__}, not an object"
        )

    version = payload.get("format")
    if version != TRANSCRIPT_FORMAT:
        raise TranscriptError(f"unknown transcript format {version!r}")

    messages = payload.get("messages")
    if not isinstance(messages, list):
        raise TranscriptError("transcript has no message list")

    return payload


def store_transcript(session_id: str, messages: Sequence[Any]) -> BlobRef:
    """Serialise and store, returning where the bytes landed.

    The ref is what goes in the manifest. A caller that only has the
    hash can still ask `import_transcript` for the content later.
    """
    data = serialize_transcript(session_id, messages)
    if len(data) > TRANSCRIPT_WARN_BYTES:
        logger.warning(
            f"transcript for {session_id} is {len(data) // 1024} KB, over the "
            f"{TRANSCRIPT_WARN_BYTES // 1024} KB advisory; syncing it anyway - "
            "a conversation too big to keep is not one to discard"
        )
    return store_bytes(data, f"{str(session_id).replace(':', '-')}.json")


def transcript_pending(sha256: Optional[str]) -> bool:
    """True when the manifest names bytes this device does not hold."""
    return bool(sha256) and not blob_exists(str(sha256))


async def read_transcript(sha256: str) -> Optional[Dict[str, Any]]:
    """Load a stored transcript, or None when the bytes are not here yet."""
    path = resolve_blob_path(sha256)
    if not path:
        return None
    try:
        with open(path, "rb") as handle:
            return parse_transcript(handle.read())
    except TranscriptError as exc:
        logger.warning(f"stored transcript {sha256[:12]} is unusable: {exc}")
        return None
    except OSError as exc:
        logger.warning(f"cannot read transcript blob {sha256[:12]}: {exc}")
        return None


async def import_transcript(session_id: str, sha256: str) -> Tuple[int, int]:
    """Merge a transcript into the local session. Returns (added, seen).

    An append-only union on record id. A message already present is
    skipped, which is what makes a repeated sync harmless and what lets
    both devices extend one conversation without either losing work.

    Original `seq` and `created` are preserved rather than recomputed:
    renumbering on arrival would reorder a conversation the peer had
    already ordered, and the local order is display order.
    """
    from open_notebook.database.repository import ensure_record_id, repo_query

    payload = await read_transcript(sha256)
    if payload is None:
        return (0, 0)

    messages = payload.get("messages") or []
    existing = await repo_query(
        "SELECT VALUE id FROM chat_message WHERE session = $sid",
        {"sid": ensure_record_id(session_id)},
    )
    # Full record ids, as everywhere else: comparing a bare key against a
    # stored `chat_message:<key>` matches nothing, which would make every
    # sync re-insert the entire conversation.
    have = {str(r) for r in (existing or [])}

    added = 0
    for row in messages:
        raw_id = str(row.get("id") or "").strip()
        if not raw_id:
            # An id is what makes the union idempotent. Without one the
            # row can only be inserted, and a second sync duplicates it.
            logger.warning(f"transcript row in {session_id} has no id; skipping")
            continue
        record_id = (
            raw_id if raw_id.startswith("chat_message:") else f"chat_message:{raw_id}"
        )
        if record_id in have:
            continue

        content = row.get("content")
        if not isinstance(content, str) or not content.strip():
            continue
        have.add(record_id)

        # The transported timestamp, falling back to now only when the
        # sender predates the field. Stamping unconditionally would leave
        # both sides disagreeing by the transfer delay, forever.
        raw_updated = row.get("updated")
        try:
            updated = datetime.fromisoformat(str(raw_updated)) if raw_updated else None
        except ValueError:
            updated = None
        if updated is None:
            updated = datetime.now(timezone.utc)
        await repo_query(
            """
            CREATE $id CONTENT {
                session: $sid, seq: $seq, role: $role,
                content: $content, created: $created, updated: $updated
            }
            """,
            {
                "id": ensure_record_id(record_id),
                "sid": ensure_record_id(session_id),
                "seq": int(row.get("seq") or 0),
                "role": _normalise_role(row.get("role")),
                "content": content,
                "created": row.get("created") or None,
                "updated": updated,
            },
        )
        added += 1

    return (added, len(messages))


def _normalise_role(value: Any) -> str:
    """Coerce to a role the schema accepts.

    The column asserts membership in ['human','ai','system'], so an
    unexpected value is a write that raises rather than a row that
    quietly does not exist.
    """
    text = str(value or "").strip().lower()
    if text in {"ai", "assistant", "system", "human", "user"}:
        return {"assistant": "ai", "user": "human"}.get(text, text)
    return "human"


def _field(row: Any, name: str, default: Any = None) -> Any:
    """Read *name* from a mapping or an object, whichever *row* is.

    Both forms occur and mixing them up loses data silently. The
    checkpoint hands over LangGraph messages, which carry attributes;
    `parse_transcript` returns dicts. A version reading only attributes
    therefore turned every dict row into an empty message - a
    transcript that serialised to 6 KB instead of 410 KB, hashed
    consistently, and looked perfectly valid at every step.
    """
    if isinstance(row, dict):
        return row.get(name, default)
    return getattr(row, name, default)


def _iso(value: Any) -> Optional[str]:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value) if value else None


def transcript_hashes(entries: Iterable[Dict[str, Any]]) -> List[str]:
    """Every transcript hash named by *entries*, deduped and ordered."""
    found = {
        str(e.get("transcript_sha256")) for e in entries if e.get("transcript_sha256")
    }
    return sorted(found)
