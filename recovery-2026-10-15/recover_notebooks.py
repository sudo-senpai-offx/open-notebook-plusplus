"""Rebuild what the 2026-10-14 notebook tombstones destroyed.

What happened
-------------
The phone's first export swept 7 notebook tombstones - including the
PC's GTB204/GTB221/GTB224/GTU301/LHP412/test notebooks and the 日本語
notebook - while reporting `tombstoned: 0`, because the first of two
`sweep_notebooks` calls discarded its return value. The PC's import then
applied all 7, and `Notebook.delete()` cascade-deleted member notes and
referring sessions: Day 1, Day 2, and 4 chat sessions went with the
notebooks, plus every reference/artifact edge.

What survived: all 46 sources and their blobs, all 28 chat messages, the
2 live sessions. The 4 deleted sessions had zero messages.

Where the map comes from
------------------------
Commit 00fe176 in the sync repo - the PC's last export before the phone's
push. It records every notebook name, every note with full content, every
session with its title, and every record's notebook slugs:

  16 source slugs    GTB204 x14, LHP412 x2 (matches the relink exactly)
   2 note slugs      Day 1 + Day 2 -> 日本語勉強会-アドバンス
   4 session slugs   test x3, 日本語勉強会-アドバンス x1

Notes and sessions are recreated under their ORIGINAL record ids. That is
what makes this a recovery rather than a duplication: the phone still
holds Day 1/Day 2 under those ids, so a same-id recreation merges where
a fresh record would fork. Notebooks get new ids - the originals are not
recorded anywhere (tombstones carry slugs, the index carries names) -
and that is safe because every edge below is rebuilt by slug, which is
the cross-device identity notebooks actually have.

Every recreated record carries a fresh timestamp. The tombstone files are
still sitting in the sync repo, and a recreated record older than them
would be deleted again on the next import. Fresh means newer, and newer
means the tombstone loses the last-write-wins comparison.

Dry run by default. This rebuilds a library.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# The last export before the tombstones. Everything rebuilt here is read
# from this commit, never from the working tree, which the phone's push
# has since overwritten with versions that know nothing (slugs emptied,
# records deleted).
BASELINE_COMMIT = "00fe176"
SYNC_REPO = Path(__file__).resolve().parents[1] / "sync"


def _show(commit: str, rel: str) -> Dict[str, Any]:
    out = subprocess.run(
        ["git", "-C", str(SYNC_REPO), "show", f"{commit}:{rel}"],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(out.stdout)


def _files(commit: str, directory: str) -> List[str]:
    out = subprocess.run(
        [
            "git",
            "-C",
            str(SYNC_REPO),
            "ls-tree",
            "-r",
            "--name-only",
            commit,
            "--",
            directory,
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return [line for line in out.stdout.splitlines() if line.endswith(".json")]


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply", action="store_true", help="rebuild (default: report only)"
    )
    args = parser.parse_args()

    from open_notebook.database.repository import ensure_record_id, repo_query

    index = _show(BASELINE_COMMIT, "notebooks/index.json")
    notebooks = index.get("notebooks") or []
    print(f"  notebooks to recreate : {len(notebooks)}")
    for nb in notebooks:
        print(f"    {nb['slug'][:44]:46} {nb.get('name', '')[:40]!r}")

    notes = [_show(BASELINE_COMMIT, rel) for rel in _files(BASELINE_COMMIT, "notes")]
    print(f"  notes to recreate    : {len(notes)}")
    sessions = [_show(BASELINE_COMMIT, rel) for rel in _files(BASELINE_COMMIT, "chat")]
    dead_sessions = [s for s in sessions if not await _exists("chat_session", s["id"])]
    print(
        f"  sessions to recreate : {len(dead_sessions)} (of {len(sessions)} in manifest)"
    )

    # Edges, counted before anything is created so the report is complete
    # even in dry-run mode.
    source_links = sum(
        len(s.get("notebook_slugs") or []) for s in _all_sources_manifest()
    )
    note_links = sum(len(n.get("notebook_slugs") or []) for n in notes)
    session_links = sum(len(s.get("notebook_slugs") or []) for s in dead_sessions)
    print(
        f"  edges to recreate    : {source_links} source + {note_links} note + {session_links} session"
    )

    existing_notes = await _ids("note")
    existing_sessions = await _ids("chat_session")
    print()
    print(
        f"  notes still present  : {len(existing_notes)} (Day 1/Day 2 must be absent)"
    )
    print(f"  sessions present     : {len(existing_sessions)}")

    if not args.apply:
        print()
        print("  dry run. Re-run with --apply to rebuild.")
        return 0

    # 1. Notebooks first: everything else resolves a slug to an id made here.
    slug_to_id: Dict[str, str] = {}
    for nb in notebooks:
        rows = await repo_query(
            "CREATE notebook CONTENT { name: $name, updated: time::now() } RETURN id",
            {"name": nb["name"]},
        )
        slug_to_id[nb["slug"]] = str(rows[0]["id"])
        print(f"  notebook  {nb['slug'][:40]:42} -> {slug_to_id[nb['slug']]}")

    # 2. Notes under their original ids, so the phone's copies merge.
    # Edges are created with the record, never separately: RELATE always
    # inserts, so a re-run that re-attached edges would double them.
    for note in notes:
        if await _exists("note", note["id"]):
            print(f"  note      {note['id'][:30]:42} already present, skipping")
            continue
        await repo_query(
            "CREATE $id CONTENT { title: $title, note_type: $type, content: $content, "
            "updated: time::now() }",
            {
                "id": ensure_record_id(note["id"]),
                "title": note.get("title"),
                "type": note.get("note_type") or "human",
                "content": note.get("content") or "",
            },
        )
        for slug in note.get("notebook_slugs") or []:
            if slug in slug_to_id:
                await repo_query(
                    "RELATE $in->artifact->$out",
                    {
                        "in": ensure_record_id(note["id"]),
                        "out": ensure_record_id(slug_to_id[slug]),
                    },
                )
        print(f"  note      {note['id'][:30]:42} {note.get('title')!r}")

    # 3. Sessions under their original ids: the orphaned messages adopt
    # back onto them, because messages point at the session id and the
    # session with that id exists again.
    for session in dead_sessions:
        if await _exists("chat_session", session["id"]):
            print(f"  session   {session['id'][:30]:42} already present, reusing")
            continue
        await repo_query(
            "CREATE $id CONTENT { title: $title, model_override: $model, "
            "updated: time::now() }",
            {
                "id": ensure_record_id(session["id"]),
                "title": session.get("title"),
                "model": session.get("model_override"),
            },
        )
        for slug in session.get("notebook_slugs") or []:
            if slug in slug_to_id:
                await repo_query(
                    "RELATE $in->refers_to->$out",
                    {
                        "in": ensure_record_id(session["id"]),
                        "out": ensure_record_id(slug_to_id[slug]),
                    },
                )
        print(f"  session   {session['id'][:30]:42} {session.get('title')!r}")

    # 4. Source grouping, by slug, for sources that exist here.
    # Existing edges are read first: RELATE always inserts, so creating
    # blindly would double every edge on a re-run.
    have_sources = await _ids("source")
    have_edges = {
        (str(r.get("in")), str(r.get("out")))
        for r in (await repo_query("SELECT in, out FROM reference") or [])
    }
    relinked = 0
    for entry in _all_sources_manifest():
        if entry["id"] not in have_sources:
            continue
        for slug in entry.get("notebook_slugs") or []:
            if slug not in slug_to_id:
                continue
            pair = (entry["id"], slug_to_id[slug])
            if pair in have_edges:
                continue
            await repo_query(
                "RELATE $in->reference->$out",
                {
                    "in": ensure_record_id(entry["id"]),
                    "out": ensure_record_id(slug_to_id[slug]),
                },
            )
            have_edges.add(pair)
            relinked += 1
    print(f"  source edges relinked: {relinked}")

    # 5. Orphans adopted back?
    orphans = await repo_query(
        "SELECT count() AS n FROM chat_message WHERE session NOT IN "
        "(SELECT VALUE id FROM chat_session) GROUP ALL"
    )
    print(
        f"  orphan messages left : {orphans[0]['n'] if orphans else 0} (want 0, modulo pre-existing phantoms)"
    )
    return 0


async def _exists(table: str, record_id: str) -> bool:
    # ensure_record_id, not the raw string: a plain-string parameter
    # against a record id silently matches nothing, so this reports
    # absent for a record that exists - and the caller recreates it.
    # That is how the first run died halfway, and the second.
    from open_notebook.database.repository import ensure_record_id, repo_query

    rows = await repo_query(
        f"SELECT VALUE id FROM {table} WHERE id = $id",  # noqa: S608
        {"id": ensure_record_id(record_id)},
    )
    return bool(rows)


async def _ids(table: str) -> set[str]:
    from open_notebook.database.repository import repo_query

    return {str(r) for r in (await repo_query(f"SELECT VALUE id FROM {table}") or [])}


def _all_sources_manifest() -> List[Dict[str, Any]]:
    return [_show(BASELINE_COMMIT, rel) for rel in _files(BASELINE_COMMIT, "sources")]


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
