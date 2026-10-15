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
Only sources with empty `full_text` AND no command reference: records
that have never been processed. A source whose job already ran - even
one that failed - is left for an explicit retry, because re-running
extraction blindly risks duplicating embeddings, and a failed job
deserves a look rather than a silent requeue. Imported sources always
qualify: they arrive with neither text nor command.

Transformations are not re-run here (empty list). The goal is readable
text plus searchable embeddings; transformations are the owner's later
choice, not a sync side effect. Notebook scope comes from current edges,
so insights generate against the same filing the record already has.
"""

from __future__ import annotations

from typing import Dict, List

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
    skipped_have_text = 0
    skipped_have_job = 0
    skipped_no_file = 0

    for row in rows:
        text = (row.get("full_text") or "").strip()
        if text:
            skipped_have_text += 1
            continue
        if row.get("command"):
            skipped_have_job += 1
            continue

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
        "skipped_have_text": skipped_have_text,
        "skipped_have_job": skipped_have_job,
        "skipped_no_file": skipped_no_file,
    }
