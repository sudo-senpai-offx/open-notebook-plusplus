"""The analyses of the parts of a long document, kept so a long transformation
survives an interruption.

A transformation of a long document reads it in parts, and each part is a
generation call. Measured on the hardware this was written on: a 46,000-token
book is ten parts, and each part is a couple of minutes of prompt evaluation
plus a few minutes of generation, so one transformation is over an hour and
the three of them are most of a day.

An interruption in the middle of that throws all of it away. The next attempt
starts again at the first part - and the interruption that mattered was the
device being restarted, which on a phone is a matter of the battery, and
which happens again, and again.

So each part's analysis is kept as it is produced, keyed by the source and
the transformation it belongs to, and a restarted transformation picks up
where it left off.

The partials are the document's content, so they are kept in the database -
which is per device - and not in the sync repository, which carries metadata
and hashes only.

The content is hashed and stored with them, so a source whose text has been
re-extracted does not resume onto the analysis of the text it used to have.
"""

from __future__ import annotations

import hashlib
from typing import Dict

from loguru import logger

from open_notebook.database.repository import (
    ensure_record_id,
    repo_delete,
    repo_query,
    repo_upsert,
)

TABLE = "transformation_partial"


def content_hash(text: str) -> str:
    """A short hash of the text the parts were analysed from."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]


def _record_id(source_id: str, transformation_id: str) -> str:
    """A deterministic id, so the partials are one record and not a pile.

    A record per part would work too, but then reading them back is a scan
    and clearing them is a delete-many, and neither of those is a thing that
    should be able to go wrong at the end of a long job.
    """
    digest = hashlib.sha256(
        f"{source_id}|{transformation_id}".encode("utf-8")
    ).hexdigest()[:32]
    return f"{TABLE}:{digest}"


async def load(
    source_id: str, transformation_id: str, text: str, split_key: str
) -> Dict[int, str]:
    """The parts already analysed, by their position in the document.

    Empty when the document has changed since the parts were analysed - the
    analyses are of the text that was there, and a different text needs
    different analyses.

    Empty also when the *split* has changed. The parts are the document cut
    up, and a different cut is a different set of parts: the first part of a
    document cut into ten is not the first part of the same document cut into
    eighteen, and resuming across that would analyse one piece of the
    document and call it another.
    """
    record_id = _record_id(source_id, transformation_id)
    try:
        rows = await repo_query(
            "SELECT partials, content_hash, split_key FROM $id",
            {"id": ensure_record_id(record_id)},
        )
    except Exception as e:  # noqa: BLE001 - a missing record is not an error
        logger.debug(f"no partials to resume for {record_id}: {e}")
        return {}
    if not rows:
        return {}
    row = rows[0]
    if row.get("content_hash") != content_hash(text):
        logger.info(
            "the source's text has changed since the parts were analysed, so "
            "the analyses are not resumed"
        )
        return {}
    if row.get("split_key") != split_key:
        logger.info(
            "the document is being split differently than it was when the parts "
            "were analysed, so the analyses are not resumed"
        )
        return {}
    stored = row.get("partials") or {}
    result: Dict[int, str] = {}
    for key, value in stored.items():
        try:
            result[int(key)] = str(value)
        except (TypeError, ValueError):
            continue
    return result


async def store(
    source_id: str,
    transformation_id: str,
    text: str,
    split_key: str,
    index: int,
    partial: str,
) -> None:
    """Keep one part's analysis, so a restart does not throw it away."""
    record_id = _record_id(source_id, transformation_id)
    existing = await load(source_id, transformation_id, text, split_key)
    existing[index] = partial
    await repo_upsert(
        TABLE,
        record_id,
        {
            "source": source_id,
            "transformation": transformation_id,
            "content_hash": content_hash(text),
            "split_key": split_key,
            "partials": {str(k): v for k, v in existing.items()},
        },
    )


async def clear(source_id: str, transformation_id: str) -> None:
    """Forget the parts, once the answer they make has been written."""
    record_id = _record_id(source_id, transformation_id)
    try:
        await repo_delete(ensure_record_id(record_id))
    except Exception as e:  # noqa: BLE001 - nothing to clear is not an error
        logger.debug(f"no partials to clear for {record_id}: {e}")
