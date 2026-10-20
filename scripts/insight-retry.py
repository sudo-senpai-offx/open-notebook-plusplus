#!/usr/bin/env python3
"""Keep the insight corpus complete, by re-queuing what failed.

    python scripts/insight-retry.py                 # run until the corpus is whole
    python scripts/insight-retry.py --once          # one pass
    python scripts/insight-retry.py --interval 300  # seconds between passes

Why this exists
---------------
`POST /api/processing/ensure-insights` is idempotent: it queues every missing
(source, transformation) pair and skips the ones that already have an insight.
So a single call is enough to fill the gaps.

But the drain still needed a human. On this machine ollama cycles its model
runner under memory pressure - a ten-second reload - and in-flight requests fail
with `Could not connect to the AI provider`. The framework retries inside one
execution, but a command that *ends* `failed` is never re-admitted: the
dispatcher admits `held` and `new`, and `failed` is history. So each blip
permanently lost an insight, and the corpus only advanced when someone noticed
and called the endpoint again.

This closes that loop. It re-queues the gaps on a timer, and stops when either
the corpus is whole or it has stopped making progress - so a pair that fails for
a reason nobody can fix is retried a bounded number of times rather than for
ever.

The progress bound matters. Without it this would re-queue one impossible pair
every five minutes for the lifetime of the deployment, and the command log
would grow without bound. With it, "no progress across N consecutive passes"
ends the run and says so.

It does not restart the app container or touch the scheduler. Those are the
things that kill in-flight work, and a supervisor that kills the jobs it is
supervising would be worse than no supervisor.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import time
from typing import Any, Dict, Tuple

WANTED = ["Dense Summary", "Key Insights", "Reflection Questions"]


def _load_env() -> None:
    """The .env next to the repo, if there is one.

    The password and the API URL live there and are not in the environment when
    the script is run by hand. Without them the endpoint answers 401 and the
    run does nothing, silently.
    """
    from pathlib import Path

    env_file = Path(__file__).resolve().parent.parent / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


async def _insight_count() -> Tuple[int, int]:
    """(have, want) over the sources that can produce an insight."""
    from open_notebook.database.repository import repo_query

    transforms = await repo_query(
        "SELECT id FROM transformation WHERE title IN $t", {"t": WANTED}
    )
    sources = await repo_query("SELECT id FROM source")
    have_rows = await repo_query("SELECT source, insight_type FROM source_insight")
    have = {
        (str(r.get("source")), str(r.get("insight_type")))
        for r in have_rows
        if str(r.get("insight_type")) in WANTED
    }
    want = len(sources) * len(transforms)
    return len(have), want


async def _pending_count() -> int:
    """How much work is still in flight.

    A pass is only idle if nothing is queued. Counting a pass as idle while
    fifteen transformations are running is the difference between waiting for
    them and giving up on them: a transformation takes about eleven minutes and
    the window admits three at a time, so eighteen of them need roughly an
    hour. The first version of this loop gave up after twenty-eight minutes of
    no *completed* insight, with work in flight the whole time - and then the
    work completed anyway, moments after it stopped.
    """
    from open_notebook.database.repository import repo_query

    rows = await repo_query(
        "SELECT count() AS n FROM command WHERE status IN ['held', 'running', 'new']"
    )
    return int(rows[0].get("n") or 0) if rows else 0


async def _queue_missing() -> Dict[str, Any]:
    """Ask the real endpoint to queue the gaps. Never reimplements it."""
    import httpx

    from open_notebook.database.repository import repo_query

    transforms = await repo_query(
        "SELECT id FROM transformation WHERE title IN $t", {"t": WANTED}
    )
    ids = [str(r["id"]) for r in transforms]
    if not ids:
        return {"error": "no transformations found"}

    password = os.environ.get("OPEN_NOTEBOOK_PASSWORD", "")
    if not password:
        return {"error": "OPEN_NOTEBOOK_PASSWORD is unset"}
    api = os.environ.get("OPEN_NOTEBOOK_API_URL", "http://127.0.0.1:5055")

    async with httpx.AsyncClient(timeout=180.0) as client:
        response = await client.post(
            f"{api}/api/processing/ensure-insights",
            json={"transformation_ids": ids},
            headers={"Authorization": f"Bearer {password}"},
        )
    if response.status_code != 200:
        return {"error": f"HTTP {response.status_code}"}
    try:
        return response.json()
    except ValueError:
        return {"error": "non-JSON response"}


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="one pass and stop")
    parser.add_argument("--interval", type=int, default=300)
    parser.add_argument(
        "--max-idle-passes",
        type=int,
        default=4,
        help="stop after this many passes with no progress",
    )
    parser.add_argument("--max-passes", type=int, default=200)
    args = parser.parse_args()

    _load_env()
    os.environ.setdefault(
        "SURREAL_URL", os.environ.get("SURREAL_URL", "ws://127.0.0.1:8108/rpc")
    )

    have, want = await _insight_count()
    print(f"  insights {have}/{want}", flush=True)
    if have >= want:
        print("  already complete", flush=True)
        return 0

    idle = 0
    passes = 0
    best = have
    while passes < args.max_passes:
        passes += 1
        result = await _queue_missing()
        queued = result.get("queued", 0) if isinstance(result, dict) else 0
        if isinstance(result, dict) and result.get("error"):
            print(f"  pass {passes}: {result['error']}", flush=True)
        else:
            print(f"  pass {passes}: queued {queued}", flush=True)

        if args.once:
            return 0

        # Give the queue time to work before judging whether it is making
        # progress. The window has to exceed a single transformation - about
        # eleven minutes at the measured rate - or every pass would look idle.
        time.sleep(max(60, args.interval))
        have, want = await _insight_count()
        pending = await _pending_count()
        print(f"  insights {have}/{want}  in flight {pending}", flush=True)
        if have >= want:
            print("  complete", flush=True)
            return 0
        if have > best:
            best = have
            idle = 0
        elif pending > 0:
            # Work is in flight, so this pass says nothing about whether the
            # corpus can advance. Waiting is the right move, and counting it as
            # idle is what made the loop give up on a healthy queue.
            idle = 0
            print(
                f"  {pending} job(s) in flight - waiting rather than counting "
                "this as idle",
                flush=True,
            )
        else:
            idle += 1
            if idle >= args.max_idle_passes:
                print(
                    f"  no progress across {idle} passes with nothing in "
                    "flight - stopping rather than re-queueing work that "
                    "cannot succeed",
                    flush=True,
                )
                return 1
    print(f"  gave up after {passes} passes", flush=True)
    return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
