"""Converge the triplicated notebooks from the recovery runs.

Why this exists
---------------
The recovery script died twice halfway (a string-vs-record-id comparison
that silently matches nothing - the same SurrealDB gotcha as ever), and
an earlier patch that would have made re-runs resume printed success
without applying. Each run therefore created a fresh set of 7 notebooks,
so three overlapping sets exist and the rebuilt edges are split across
them: notes point at a run-1 日本語, sources and one session at run-3
notebooks, and the 3 test-session edges never landed anywhere.

What it does
-------------
Keeps exactly one notebook per name - preferring the copy that already
holds edges - moves the single stray session edge onto the kept 日本語,
creates the 3 missing test-session edges, and deletes the 12 edgeless
duplicates with `repo_delete`.

`repo_delete`, explicitly not `Notebook.delete()`: the method
cascade-deletes member notes and referring sessions, which is how a
7-notebook tombstone application became a 13-record data loss event.
These notebooks are edgeless (verified before each delete), so there is
nothing to cascade to - but the method would still be the wrong tool even
if there were. Deleting a folder must never destroy its contents; that
is the lesson of this whole incident.

Dry run by default.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# session id -> notebook name, for edges the recovery never created.
MISSING_SESSION_EDGES = {
    "chat_session:dzk7tfxgouj7oq3qhlcs": "test",
    "chat_session:me15891vxufm9iqmf68z": "test",
    "chat_session:p4dp78tpcwpbpanwb97c": "test",
}

# (session id, wrong notebook id will be resolved at runtime) -> keep.
MOVE_SESSION_EDGE = (
    "chat_session:qcz36euh403z9n2x0d6c",
    "日本語勉強会   ||   アドバンス",
)


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply", action="store_true", help="converge (default: report only)"
    )
    args = parser.parse_args()

    from open_notebook.database.repository import (
        ensure_record_id,
        repo_delete,
        repo_query,
    )

    notebooks = await repo_query("SELECT id, name FROM notebook") or []
    by_name: dict[str, list[str]] = {}
    for row in notebooks:
        by_name.setdefault(row.get("name") or "?", []).append(str(row["id"]))

    async def edges(nbid: str) -> dict[str, int]:
        rid = ensure_record_id(nbid)
        out: dict[str, int] = {}
        for table, direction in (
            ("artifact", "out"),
            ("reference", "out"),
            ("refers_to", "out"),
        ):
            rows = await repo_query(
                f"SELECT count() AS c FROM {table} WHERE {direction} = $id GROUP ALL",  # noqa: S608
                {"id": rid},
            )
            out[table] = rows[0]["c"] if rows else 0
        return out

    keep: dict[str, str] = {}
    drop: list[str] = []
    print("  keeper selection (most edges wins):")
    for name, ids in sorted(by_name.items()):
        scored = [(sum((await edges(i)).values()), i) for i in ids]
        scored.sort(reverse=True)
        keep[name] = scored[0][1]
        for score, nid in scored:
            mark = "KEEP" if nid == scored[0][1] else "drop"
            print(f"    {mark}  {nid[:30]:32} edges={score}  {name[:30]!r}")
            if nid != scored[0][1]:
                drop.append(nid)

    print()
    print(f"  keep {len(keep)}, drop {len(drop)}")
    if not args.apply:
        print("  dry run. Re-run with --apply to converge.")
        return 0

    # 1. Move the stray session edge onto the kept 日本語.
    session_id, notebook_name = MOVE_SESSION_EDGE
    target = keep[notebook_name]
    await repo_query(
        "DELETE refers_to WHERE in = $sid",
        {"sid": ensure_record_id(session_id)},
    )
    await repo_query(
        "RELATE $in->refers_to->$out",
        {
            "in": ensure_record_id(session_id),
            "out": ensure_record_id(target),
        },
    )
    print(f"  moved   {session_id[:30]:32} -> kept 日本語")

    # 2. Create the test-session edges the recovery may not have made.
    # Checked first, not blindly: an earlier listing was truncated by
    # `tail` and hid the notebook holding them, which made them look
    # missing when they were merely unseen. RELATE always inserts, so
    # creating on a guess would double them.
    for sid, name in MISSING_SESSION_EDGES.items():
        existing = await repo_query(
            "SELECT VALUE out FROM refers_to WHERE in = $sid",
            {"sid": ensure_record_id(sid)},
        )
        if existing:
            print(f"  present {sid[:30]:32} -> {str(existing[0])[:30]}")
            continue
        await repo_query(
            "RELATE $in->refers_to->$out",
            {
                "in": ensure_record_id(sid),
                "out": ensure_record_id(keep[name]),
            },
        )
        print(f"  linked  {sid[:30]:32} -> kept test")

    # 3. Delete the edgeless duplicates. Verified edgeless first, one by
    # one: a notebook that gained an edge since the report above must
    # not be deleted on stale information.
    for nid in drop:
        counts = await edges(nid)
        if sum(counts.values()) > 0:
            print(f"  SKIP    {nid[:30]:32} gained edges {counts}, keeping")
            continue
        await repo_delete(nid)
        print(f"  deleted {nid[:30]:32} edgeless duplicate")

    # 4. Verify the converged state.
    final = await repo_query("SELECT id, name FROM notebook") or []
    ref = await repo_query("SELECT VALUE count() FROM reference GROUP ALL")
    art = await repo_query("SELECT VALUE count() FROM artifact GROUP ALL")
    rft = await repo_query("SELECT VALUE count() FROM refers_to GROUP ALL")
    print()
    print(f"  notebooks={len(final)} (want 7)")
    print(f"  reference={ref[0]['count'] if ref else 0} (want 16)")
    print(
        f"  artifact={art[0]['c'] if art and 'c' in art[0] else art[0]['count'] if art else 0} (want 2)"
    )
    print(f"  refers_to={rft[0]['count'] if rft else 0} (want 4)")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
