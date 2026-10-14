"""Re-attach recovered sources to the notebooks they belong to.

Why this exists
---------------
The 2026-10-14 data-loss incident was recovered by scripts that read the
decoded records and wrote them straight into the database. None of them
ran inside a notebook context, so every recovered source came back with
no `reference` edge: 46 sources, 7 notebooks, **0** links.

That is not cosmetic. Notebook-scoped retrieval resolves sources through
`SELECT VALUE in FROM reference WHERE out IN $notebook_ids`, so an
unlinked source is invisible to chat - it cannot be found, cited, or
used to answer anything, despite existing and being fully embedded.

Adding a source through the UI has always linked it correctly. This
repairs the ones that came back through the wrong door.

Scope of this script
--------------------
Deliberately narrow and deliberately dull. It links only where the
source's own title states the subject - there is no inference, no
fuzzy matching, and no keyword guessing. Anything it is not certain
about is left alone and reported, because a source filed under the
wrong course is worse than an unfiled one: it will be confidently
retrieved for the wrong question.

    python relink_sources.py                  # dry run, changes nothing
    python relink_sources.py --apply
    python relink_sources.py --delete-unplaced --apply

`--delete-unplaced` removes the sources that matched no course. Unlike
the others these hold no file - they are crawled links whose URL is the
entire content - so deleting them is not reversible by any means, only
undoable by re-crawling the URL. That is why it is a separate flag and
not part of `--apply`.

Deletes are `Source.delete()`, the same call the UI makes, so the blob
survives (auto_delete_files defaults to no) and the next export
tombstones the record for the other device. Deleting is therefore
visible on the phone - which is the point, but worth knowing before
running with --apply.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from typing import Dict, List, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from open_notebook.database.repository import ensure_record_id, repo_query  # noqa: E402
from open_notebook.domain.notebook import Source  # noqa: E402

# Where each group belongs. Keyed by notebook name; matched exactly, so a
# renamed notebook is a loud miss rather than a silent misfiling.
GTB204 = "GTB204 - Molecular Biology Techniques"
LHP412 = "LHP412 - Effective Oral Presentation"

# Subject phrases that appear *in the source's own title*. Nothing here
# is a guess about content: each phrase is the topic the document is
# titled after.
MOLECULAR_BIOLOGY_TERMS = (
    "genomic library",
    "cdna library",
    "intro to molecular",
    "molecular biology",
    "production of protein",
    "protein purification",
    "prokaryotic gene",
    "eukaryotic expression",
    "eukaryote prokaryote",
    "gene transfer",
    "crispr",
    "bioinformatic",
    "electrophoretic",
    "component of the immune",
)

COMMUNICATION_TERMS = (
    "effective communication",
    "effective oral presentation",
    "21st century communication",
    "communication_z_library",
)

# Test scaffolding left behind by perf runs. Deleted, not filed.
JUNK_PREFIXES = ("fixture-throttle-", "perf-big3-")


def classify(title: str) -> str:
    """Return the notebook name, "delete", or "" to leave it alone."""
    lowered = (title or "").strip().lower()
    if any(lowered.startswith(prefix) for prefix in JUNK_PREFIXES):
        return "delete"
    if any(term in lowered for term in MOLECULAR_BIOLOGY_TERMS):
        return GTB204
    if any(term in lowered for term in COMMUNICATION_TERMS):
        return LHP412
    return ""


async def notebook_by_name(name: str) -> str | None:
    rows = await repo_query(
        "SELECT id, name FROM notebook WHERE name = $name", {"name": name}
    )
    if not rows:
        return None
    return str(rows[0]["id"])


async def already_linked(source_id: str, notebook_id: str) -> bool:
    rows = await repo_query(
        "SELECT * FROM reference WHERE in = $sid AND out = $nid",
        {"sid": ensure_record_id(source_id), "nid": ensure_record_id(notebook_id)},
    )
    return bool(rows)


async def main(apply: bool, delete_unplaced: bool = False) -> int:
    targets: Dict[str, str] = {}
    missing: List[str] = []
    for name in (GTB204, LHP412):
        found = await notebook_by_name(name)
        if found:
            targets[found] = name
        else:
            missing.append(name)

    if missing:
        print(f"  FATAL: notebook(s) not found: {', '.join(missing)}")
        print("  Nothing was changed. Check the names in relink_sources.py.")
        return 1

    rows = await repo_query("SELECT id, title FROM source") or []
    plan: List[Tuple[str, str, str]] = []  # (source_id, title, action)
    unplaced: List[str] = []

    for row in rows:
        title = (row.get("title") or "").strip()
        verdict = classify(title)
        if not verdict:
            if delete_unplaced:
                plan.append((str(row["id"]), title, "delete-unplaced"))
            else:
                unplaced.append(title)
            continue
        plan.append((str(row["id"]), title, verdict))

    print(f"  notebooks: {', '.join(sorted(targets.values()))}")
    print(f"  sources scanned: {len(rows)}")
    print(f"  will act on    : {len(plan)}")
    print(f"  leaving alone  : {len(unplaced)}")
    print()

    if not apply:
        print("  DRY RUN - nothing changed. Re-run with --apply.\n")

    for source_id, title, verdict in plan:
        if verdict in ("delete", "delete-unplaced"):
            print(f"  DELETE   {title[:62]}")
            if apply:
                await (await Source.get(source_id)).delete()
            continue

        notebook_id = next(nid for nid, nname in targets.items() if nname == verdict)
        already = await already_linked(source_id, notebook_id)
        verb = "LINK" if not already else "skip (already linked)"
        print(f"  {verb:<8} {verdict.split(' - ')[0]:<7} {title[:48]}")
        if apply and not already:
            await repo_query(
                "RELATE $sid->reference->$nid",
                {
                    "sid": ensure_record_id(source_id),
                    "nid": ensure_record_id(notebook_id),
                },
            )

    print()
    if unplaced:
        print(f"  left unplaced ({len(unplaced)}):")
        for title in sorted(unplaced):
            print(f"      {title[:70]}")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="actually write")
    parser.add_argument(
        "--delete-unplaced",
        action="store_true",
        help="also delete sources that matched no course (irreversible: they are links)",
    )
    args = parser.parse_args()
    raise SystemExit(asyncio.run(main(args.apply, args.delete_unplaced)))
