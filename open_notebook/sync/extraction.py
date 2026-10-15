"""Extract text (and embeddings) for sources whose bytes arrived late.

Why this exists
--------------
A synced source arrives as metadata first and bytes later, by design.
But nothing in that design extracted the text when the bytes landed:
import creates the record with no `full_text` (there is no file yet),
the blob channel delivers the file, and no hook connects the two. The
result is a library that reports `sources_awaiting_blobs: 0` - every
hash resolves - while every file opens as "No content available",
because the detail view reads `full_text` and it was never written.

So blob arrival has to schedule processing the way upload does. This
module scans for sources that have a resolvable file but no text and
submits the same `process_source` job an upload submits, with the blob
path as its `content_state`.

What is queued, and what is not
-------------------------------
Only sources with empty `full_text`. Of those, skipped when a live
`process_source` job exists (new or running - re-queueing would double
the work) or when one's record says failed or canceled (an explicit
signal that deserves a look and an explicit retry, not a silent loop
burning a phone's CPU on a file that cannot be read).

Everything else queues: never-run, a reference whose job record is
gone (retention prunes old commands, and a pruned record must not strand
its file forever), a completed job that left no text, or a reference to
some other command kind (embed jobs do not write text). Reprocessing is
safe to repeat: extraction overwrites `full_text` and the embed stage
deletes existing embeddings before recreating them.

Imported sources always qualify on first sight: they arrive with neither
text nor command.

Transformations are not re-run here (empty list). The goal is readable
text plus searchable embeddings; transformations are the owner's later
choice, not a sync side effect. Notebook scope comes from current edges,
so insights generate against the same filing the record already has.
"""

from __future__ import annotations

from typing import Dict, List, Optional

from loguru import logger


async def enqueue_missing_extraction(limit: int = 0) -> Dict[str, object]:
    """Submit `process_source` for unprocessed sources that have files.

    Returns what was queued and what was skipped, for the caller to
    report. Never raises for a single bad record: one malformed source
    must not strand the fourteen behind it.
    """
    import commands.source_commands  # noqa: F401
    from api.command_service import CommandService
    from commands.source_commands import SourceProcessingInput
    from open_notebook.database.repository import ensure_record_id, repo_query
    from open_notebook.storage.blobs import resolve_blob_path

    rows = (
        await repo_query("SELECT id, title, asset, full_text, command FROM source")
        or []
    )

    queued: List[str] = []
    needs_retry: List[str] = []
    skipped_have_text = 0
    skipped_have_job = 0
    skipped_no_file = 0

    for row in rows:
        text = (row.get("full_text") or "").strip()
        if text:
            skipped_have_text += 1
            continue
        record_id = str(row.get("id"))
        if row.get("command"):
            reason = await _skip_reason(str(row["command"]))
            if reason == "in flight":
                skipped_have_job += 1
                continue
            if reason == "needs a look":
                # Failed or canceled: a human decision, not an automatic
                # requeue that burns a phone's CPU on an unreadable file.
                # Recorded so the caller can show which files are waiting
                # on their owner.
                skipped_have_job += 1
                needs_retry.append(record_id)
                continue
            # No record (retention pruned it), a completed job that left
            # no text, or some other command kind: none of these wrote
            # text, so all fall through to the queue below.

        asset = row.get("asset") or {}
        if not asset.get("sha256"):
            # Links and text sources: nothing to extract from.
            skipped_no_file += 1
            continue
        file_path = resolve_blob_path(asset.get("sha256"), asset.get("filename"))
        if not file_path:
            skipped_no_file += 1
            continue

        record_id = str(row.get("id"))
        try:
            # Two queries, not a UNION: SurrealDB's set operations are
            # version-sensitive, and two plain selects are not.
            notebooks = await repo_query(
                "SELECT VALUE out FROM reference WHERE in = $sid",
                {"sid": ensure_record_id(record_id)},
            )
            notebooks = (notebooks or []) + await repo_query(
                "SELECT VALUE out FROM artifact WHERE in = $sid",
                {"sid": ensure_record_id(record_id)},
            )
            command_id = await CommandService.submit_command_job(
                "open_notebook",
                "process_source",
                SourceProcessingInput(
                    source_id=record_id,
                    content_state={"file_path": file_path},
                    notebook_ids=[str(n) for n in (notebooks or [])],
                    transformations=[],
                    embed=True,
                ).model_dump(),
            )
        except Exception as exc:  # noqa: BLE001 - one bad record strands nothing
            logger.warning(f"could not queue extraction for {record_id}: {exc}")
            continue

        try:
            await repo_query(
                "UPDATE $id SET command = $cmd",
                {
                    "id": ensure_record_id(record_id),
                    "cmd": ensure_record_id(command_id),
                },
            )
        except Exception as exc:  # noqa: BLE001 - the job is queued either way
            logger.warning(f"could not stamp command on {record_id}: {exc}")

        queued.append(record_id)
        if limit and len(queued) >= limit:
            break

    return {
        "queued": queued,
        "queued_count": len(queued),
        "needs_retry": needs_retry,
        "skipped_have_text": skipped_have_text,
        "skipped_have_job": skipped_have_job,
        "skipped_no_file": skipped_no_file,
    }


async def _skip_reason(command_ref: str) -> Optional[str]:
    """Why not queue over this command reference, or None to queue.

    `in flight` - a live `process_source` job exists; re-queueing would
    double the work. `needs a look` - it failed or was canceled, which
    is an explicit signal deserving eyes before any retry. Anything
    else (no record, completed without text, another command kind)
    means no text was ever written by it, so queue.
    """
    from open_notebook.database.repository import ensure_record_id, repo_query

    try:
        rows = await repo_query(
            "SELECT name, status FROM command WHERE id = $cid",
            {"cid": ensure_record_id(command_ref)},
        )
    except Exception as exc:  # noqa: BLE001 - unreadable is queueable
        logger.warning(f"could not read command {command_ref}: {exc}")
        return None
    if not rows:
        return None
    job = rows[0]
    if (job.get("name") or "") != "process_source":
        return None
    if job.get("status") in ("new", "running"):
        return "in flight"
    if job.get("status") in ("failed", "canceled"):
        return "needs a look"
    return None
