"""Record deletions so they survive a round trip.

Without this, deleting a source on the phone is undone by the next
export from the PC: the PC still holds the record, writes it back, and
the phone's import recreates it. The user sees a deletion silently
un-happen.

The absence of a file is not information. Git cannot tell "never
exported" from "deleted", so deletion has to be written down
explicitly. That written-down fact is a tombstone.

    {"id": "source:abc", "kind": "source",
     "updated": "2026-10-14T18:30:00+00:00",
     "sha256": "9f2c..." }

### Where tombstones come from

Not from the delete methods. From the export sweep, which compares the
records that exist now against the files the manifest already holds.
Anything that vanished is deleted, however it was deleted - the UI
route, the notebook cascade, a script, a future code path. Poking five
delete methods would cover four of them and quietly miss the fifth.

### How they resolve

By the same rule everything else already uses: last-write-wins on
`updated`. A tombstone newer than the local record deletes it; a local
record newer than the tombstone ignores it, so editing a source after
someone else deleted it brings it back. That is the rule the user asked
for, and it is what "most recent activity wins, regardless of device"
means.

A tombstone is also stamped *after* the record it deletes. Clocks on two
devices are not synchronised, and a phone whose clock runs an hour
behind would otherwise lose its own deletion to a PC record it deleted
minutes ago. Taking `max(now, record.updated + 1s)` makes the local
delete authoritative regardless of what the other clock says.

### Retention

Tombstones are kept indefinitely by default. They are a few dozen bytes
each and they ride in git, so the cost is noise. The reason not to
prune them is not disk - it is that a purged tombstone cannot protect
against a device that was offline while it existed. That device
re-exports its stale copy, finds nothing local to compare against, and
the record comes back from the dead. Forever is the only setting that
cannot lose a deletion.

`prune_tombstones` exists for operators who want the files gone anyway.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence

from loguru import logger

TOMBSTONES_DIR = "tombstones"

# Notebooks tombstone under a synthetic id rather than a record id;
# see `notebook_id`.
NOTEBOOKS_KIND = "notebooks"

# How far past the record's own timestamp a tombstone is stamped. One
# second is enough to win the comparison and small enough that the
# ordering still means what a person would expect.
_TOMBSTONE_MARGIN = timedelta(seconds=1)


@dataclass(frozen=True)
class Tombstone:
    """A record that was deleted, and when."""

    id: str
    kind: str
    updated: str
    sha256: Optional[str] = None

    @property
    def deleted_at(self) -> Optional[datetime]:
        return _parse(self.updated)


def _parse(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    # A naive timestamp is read as UTC rather than local time. Devices
    # are not guaranteed to agree on a timezone, and silently shifting a
    # deletion by an offset is worse than assuming the common one.
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _now_after(previous: Optional[str]) -> str:
    """Now, or one second after `previous` - whichever is later.

    Guarantees a tombstone outranks the record it deletes even when this
    device's clock is behind the other one's.
    """
    stamp = datetime.now(timezone.utc)
    earlier = _parse(previous)
    if earlier is not None and earlier + _TOMBSTONE_MARGIN > stamp:
        stamp = earlier + _TOMBSTONE_MARGIN
    return stamp.isoformat()


def _filename(record_id: str) -> str:
    """`source:abc-123` -> `source-abc-123.json`, matching the manifest."""
    return f"{record_id.replace(':', '-')}.json"


def write_tombstone(
    root: Path,
    record_id: str,
    kind: str,
    previous_updated: Optional[str] = None,
    sha256: Optional[str] = None,
) -> Tombstone:
    """Record that `record_id` is gone. Overwrites an older tombstone."""
    directory = root / TOMBSTONES_DIR
    directory.mkdir(parents=True, exist_ok=True)
    tombstone = Tombstone(
        id=str(record_id),
        kind=kind,
        updated=_now_after(previous_updated),
        sha256=sha256,
    )
    path = directory / _filename(tombstone.id)
    payload = {
        "id": tombstone.id,
        "kind": tombstone.kind,
        "updated": tombstone.updated,
        "deleted": True,
    }
    if tombstone.sha256:
        payload["sha256"] = tombstone.sha256
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)
    logger.info(f"Tombstoned {tombstone.kind} {tombstone.id} at {tombstone.updated}")
    return tombstone


def load_tombstones(root: Path) -> Dict[str, Tombstone]:
    """Every tombstone in the manifest, newest wins per record.

    A record can be deleted, resurrected by an edit, and deleted again;
    only the latest state is worth acting on.
    """
    directory = root / TOMBSTONES_DIR
    if not directory.is_dir():
        return {}

    found: Dict[str, Tombstone] = {}
    for path in sorted(directory.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.warning(f"Unreadable tombstone {path.name}: {exc}")
            continue
        if not data.get("deleted") or not data.get("id"):
            continue
        candidate = Tombstone(
            id=str(data["id"]),
            kind=str(data.get("kind") or "unknown"),
            updated=str(data.get("updated") or ""),
            sha256=data.get("sha256"),
        )
        existing = found.get(candidate.id)
        if existing is None or _later(candidate.updated, existing.updated):
            found[candidate.id] = candidate
    return found


def _later(left: Optional[str], right: Optional[str]) -> bool:
    a, b = _parse(left), _parse(right)
    if a is None:
        return False
    if b is None:
        return True
    return a > b


def sweep(
    root: Path,
    live: Dict[str, Sequence[str]],
    extra: Optional[Dict[str, Dict[str, Optional[str]]]] = None,
) -> List[Tombstone]:
    """Compare the manifest against reality and tombstone what vanished.

    *live* maps a manifest directory to the ids that currently exist.
    Every json file under it whose id is absent gets a tombstone and is
    removed, so the manifest stops carrying a record that no longer
    exists.

    *extra* carries per-id values the tombstone should keep - used for
    the `sha256` of a deleted source, so the peer knows which blob is
    now unreferenced without having to guess.
    """
    written: List[Tombstone] = []
    for directory, live_ids in live.items():
        folder = root / directory
        if not folder.is_dir():
            continue
        keep = {str(i) for i in live_ids}
        for path in sorted(folder.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                # A corrupt manifest file is not evidence of a deletion.
                logger.warning(f"Unreadable manifest file {path.name}, leaving it")
                continue
            record_id = str(data.get("id") or "")
            if not record_id or record_id in keep:
                continue
            if not is_deletable(data.get("updated")):
                continue

                # The hash comes from the manifest file, not from the
            # record: the record is gone and cannot be asked what it
            # pointed at.
            # Keeping it lets the peer report which blob lost its last
            # reference without re-reading anything.
            sha = (extra or {}).get(record_id, {}).get("sha256") or data.get("sha256")
            tombstone = write_tombstone(
                root,
                record_id,
                kind=directory,
                previous_updated=data.get("updated"),
                sha256=sha,
            )
            written.append(tombstone)
            # The stale file is removed rather than left behind: keeping
            # it would make every future import delete the record again,
            # and the tombstone is already the authoritative signal.
            path.unlink(missing_ok=True)
    return written


def notebook_id(slug: str) -> str:
    """Synthetic, stable identity for a notebook in a tombstone.

    Every other record keeps its database id, which survives the sync
    because `_create_with_id` preserves it. Notebooks cannot: they are
    matched by slug, since a record id means nothing on the other
    device. A tombstone needs an id both sides can derive, and the slug
    is the only thing they share.
    """
    return f"notebook:{slug}"


def sweep_notebooks(
    root: Path, index_rel: str, live_slugs: Iterable[str]
) -> List[Tombstone]:
    """Tombstone notebooks that vanished, and drop them from the index.

    The index is one file rather than one file per notebook, so this
    cannot reuse `sweep` - but the principle is identical: compare what
    the manifest claims against what exists now.
    """
    path = root / index_rel
    if not path.is_file():
        return []

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning(f"Unreadable notebook index {index_rel}: {exc}")
        return []

    entries = payload.get("notebooks") or []
    live = {str(s) for s in live_slugs}
    kept: List[Dict] = []
    written: List[Tombstone] = []

    for entry in entries:
        slug = str(entry.get("slug") or "")
        if slug and slug not in live and is_deletable(entry.get("updated")):
            written.append(
                write_tombstone(
                    root,
                    notebook_id(slug),
                    kind=NOTEBOOKS_KIND,
                    previous_updated=entry.get("updated"),
                )
            )
        else:
            kept.append(entry)

    if written:
        payload["notebooks"] = kept
        temporary = path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)
    return written


def prune_tombstones(root: Path, older_than_days: int) -> List[str]:
    """Drop tombstones older than the cutoff.

    Deliberately not wired into any automatic path. A tombstone that is
    removed stops being able to protect against a device that was offline
    while it existed, so this is an operator decision, made knowingly.
    """
    directory = root / TOMBSTONES_DIR
    if not directory.is_dir():
        return []

    cutoff = datetime.now(timezone.utc) - timedelta(days=older_than_days)
    removed: List[str] = []
    for path in sorted(directory.glob("*.json")):
        tombstone = load_tombstones(root).get(path.stem.replace("-", ":", 1))
        if tombstone is None or tombstone.deleted_at is None:
            continue
        if tombstone.deleted_at < cutoff:
            path.unlink(missing_ok=True)
            removed.append(tombstone.id)
    return removed


# --- the sync watermark --------------------------------------------------
#
# The sweep answers "what is in the manifest that this device no longer
# has?" and treats the answer as "was deleted here". Those are not the
# same question, and the difference is data loss.
#
# A device that has never completed an import sees the peer's whole
# library as "records I do not have" and would tombstone all of it on
# its first export. Concretely: the phone was running a build with no
# notebook sync, held zero notebooks, and the manifest carried seven.
# Its first export would have declared all seven deleted - and the PC
# would have applied that as a deletion, cascading notes with it.
#
# A tombstone is only evidence of a deletion if the tombstoning device
# once *had* the record. The watermark records when this device last
# successfully imported; anything the manifest gained after that point
# has not arrived yet and is therefore not a deletion.

WATERMARK_FILE = "sync-watermark.json"


def _watermark_path() -> Path:
    """Deliberately outside the sync directory.

    The sync directory is the shared git repo. A watermark is a fact
    about *this* installation - when this device last synced - and
    committing it would hand the peer a lie about our history.
    """
    return Path(os.getenv("DATA_FOLDER", "./data")) / WATERMARK_FILE


def read_watermark() -> Optional[datetime]:
    """When this device last completed an import, or None if never."""
    try:
        payload = json.loads(_watermark_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return _parse(payload.get("last_import_at"))


def write_watermark(when: Optional[datetime] = None) -> None:
    """Mark a completed import. Never moves backwards."""
    previous = read_watermark()
    stamp = when or datetime.now(timezone.utc)
    if previous is not None and previous > stamp:
        stamp = previous
    path = _watermark_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"last_import_at": stamp.isoformat()}, indent=2) + "\n",
        encoding="utf-8",
    )


class _Unset:
    """Sentinel, because `None` already means "this device never imported".

    Overloading None for both left `is_deletable(stamp, None)` silently
    reading the on-disk watermark, so the one case the tests most need to
    pin - a device that has never synced - was decided by whatever a
    previous test run happened to leave in `data/`.
    """

    def __repr__(self) -> str:
        return "<unset>"


_UNSET = _Unset()


def is_deletable(
    updated: Optional[str], watermark: Optional[datetime] | _Unset = _UNSET
) -> bool:
    """Whether a manifest entry may be tombstoned on this device.

    False when this device has never imported, so nothing can be
    considered deleted here. False when the entry is *newer* than the
    last import, because it has not arrived yet. False when it carries
    no timestamp at all - an older manifest cannot prove the record
    reached this device, and the safe reading of "cannot prove" is
    "do not destroy".
    """
    # Passed in so this is a pure function and can be reasoned about
    # without touching module state. Only the caller that genuinely
    # means "this device's own" leaves it unset.
    if isinstance(watermark, _Unset):
        watermark = read_watermark()
    if watermark is None:
        return False
    stamp = _parse(updated)
    if stamp is None:
        return False
    return stamp <= watermark
