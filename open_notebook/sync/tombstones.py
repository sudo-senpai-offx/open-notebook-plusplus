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
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from loguru import logger

TOMBSTONES_DIR = "tombstones"

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
