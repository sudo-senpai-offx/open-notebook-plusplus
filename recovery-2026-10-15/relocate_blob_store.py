"""Make the blob store visible where the app actually reads it.

What is wrong
-------------
Not a legacy migration. The sources were migrated to content addressing
correctly - `asset.file_path` is already `data/blobs/<aa>/<sha256>.<ext>`
- and every recorded hash is present on disk. The problem is that there
are two stores and the app is looking at the empty one:

    ./data/blobs             21 files   the real store
    notebook_data/blobs       0 files   what the container reads

The container mounts `notebook_data` at `/app/data` and resolves
`BLOBS_FOLDER` to `./data/blobs` relative to its working directory, so it
reads `notebook_data/blobs`. Twenty-one files sit in `./data/blobs`,
which is the *host's* default data folder - a different tree entirely.

So the app can open none of its own sources. Every hash resolves to
nothing, `sources_awaiting_blobs` reports all fifteen, and a sync would
hand the phone sixteen sources it cannot open - metadata that looks
perfectly healthy and is useless.

This copies the blobs into the directory the app reads. It copies and
never moves: `./data/blobs` may be a store some other process still
points at, and deleting from it is not this script's business. Duplicate
blobs cost nothing - content addressing means the same bytes occupy one
path wherever they are kept.

Every copy is verified by re-hashing the destination. A blob is only
reported as adopted once the bytes at its hash-named path actually hash
to that name, because a path that looks right and holds the wrong bytes
is the worst possible outcome: the source would open, and be wrong.

Dry run by default. This touches a library.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import shutil
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Both sides are named explicitly rather than derived from config.
#
# `BLOBS_FOLDER` is `./data/blobs` relative to whatever process asks, so
# the same setting means the host store when the host asks and the
# container's store when the container asks. Deriving the source from it
# made this script report "nothing to do" while the app could open none
# of its sources - the configuration was correct and the comparison was
# between a thing and itself.
SOURCE_ROOT = Path("data/blobs")
TARGET_ROOT = Path("notebook_data/blobs")


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _shard_map(root: Path) -> Dict[str, Path]:
    """Every blob under *root*, keyed by its hash.

    Keyed from the *filename*, then verified by content before use. A
    file whose name is not a hash is not a blob, whatever it contains.
    """
    found: Dict[str, Path] = {}
    if not root.is_dir():
        return found
    for entry in root.rglob("*"):
        if entry.is_file() and len(entry.stem) == 64:
            found.setdefault(entry.stem, entry)
    return found


async def survey() -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    from open_notebook.database.repository import repo_query

    rows = await repo_query("SELECT id, title, asset FROM source") or []
    stats = {"total": 0, "no_hash": 0, "present": 0, "missing": 0, "unreadable": 0}
    pending: List[Dict[str, Any]] = []

    target = TARGET_ROOT
    available = _shard_map(SOURCE_ROOT)

    for row in rows:
        stats["total"] += 1
        asset = row.get("asset") or {}
        digest = asset.get("sha256")
        if not digest:
            stats["no_hash"] += 1
            continue

        source = available.get(digest)
        if source is None:
            stats["missing"] += 1
            pending.append(
                {
                    "id": str(row.get("id")),
                    "title": row.get("title"),
                    "sha256": digest,
                    "status": "no copy found on this machine",
                }
            )
            continue

        # Already where the app looks? Then there is nothing to do.
        if (
            target.is_dir()
            and (target / digest[:2] / f"{digest}{source.suffix}").is_file()
        ):
            stats["present"] += 1
            continue

        pending.append(
            {
                "id": str(row.get("id")),
                "title": row.get("title"),
                "sha256": digest,
                "source": str(source),
                "target": str(target / digest[:2] / f"{digest}{source.suffix}"),
                "status": "ready",
            }
        )

    return pending, stats


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply", action="store_true", help="copy (default: report only)"
    )
    args = parser.parse_args()

    pending, stats = await survey()
    print(f"  copying from : {SOURCE_ROOT}")
    print(f"  copying to   : {TARGET_ROOT}   (what the container mounts)")
    print(f"  sources recorded         : {stats['total']}")
    print(f"  already readable there  : {stats['present']}")
    print(f"  hash recorded, no file  : {stats['missing']}")
    print(f"  readable elsewhere      : {len(pending) - stats['missing']}")
    print()

    if not pending:
        print("  nothing to do - every recorded hash is already readable.")
        return 0

    adopted = failed = 0
    for item in pending:
        label = (item.get("title") or "?")[:44]
        if item["status"] != "ready":
            print(f"  SKIP   {label:<46} {item['status']}")
            failed += 1
            continue

        source = Path(item["source"])
        target = Path(item["target"])
        if not args.apply:
            print(
                f"  copy   {label:<46} {source.stat().st_size // 1024:>5} KB  "
                f"-> {target.parent.name}/{target.name[:16]}..."
            )
            continue

        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(".partial")
        shutil.copy2(source, temporary)

        # Verify the copy, not the original. A truncated write, a full
        # disk, or a source that changed underneath us all land here.
        if _hash_file(temporary) != item["sha256"]:
            temporary.unlink(missing_ok=True)
            print(f"  FAIL   {label:<46} copy does not match its hash; discarded")
            failed += 1
            continue

        temporary.replace(target)
        print(f"  copied {label:<46} {target.stat().st_size // 1024:>5} KB")
        adopted += 1

    print()
    if not args.apply:
        print(f"  dry run. Re-run with --apply to copy {len(pending)} blob(s).")
    else:
        print(f"  copied {adopted}, could not {failed}")
        if adopted:
            print("  restart the app so it re-resolves the store:")
            print("    docker compose restart open_notebook")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
