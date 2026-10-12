#!/usr/bin/env python3
"""N2b retrieval QC runner: fixture pairs through live /search, hit table.

Usage: python scripts/retrieval_qc.py evals/quality_fixture.json
       [--api http://localhost:5055] [--k 5] [--type vector]
Skips entries marked "sample". Exit nonzero when recall < 1.0 so CI-style
gates can pin the bar later (today: informational).
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request


def post_search(api: str, query: str, limit: int, search_type: str) -> list:
    payload = {"query": query, "limit": limit, "type": search_type}
    req = urllib.request.Request(
        f"{api}/api/search",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=120) as response:
        body = json.loads(response.read().decode())
    return body.get("results") or []


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("fixture")
    parser.add_argument("--api", default="http://localhost:5055")
    parser.add_argument("--k", type=int, default=5)
    parser.add_argument("--type", default="vector")
    args = parser.parse_args()

    with open(args.fixture, encoding="utf-8") as f:
        fixture = json.load(f)
    pairs = [p for p in fixture.get("pairs", []) if not p.get("sample")]
    if not pairs:
        print("fixture has no real pairs (samples skipped); nothing to run.")
        return 0

    sys.path.insert(0, ".")
    from open_notebook.eval.qc import recall_at_k, summarize

    outcomes = []
    for pair in pairs:
        try:
            results = post_search(args.api, pair["question"], args.k, args.type)
            ids = [
                r.get("source_id") or r.get("id") or r.get("source", {}).get("id")
                if isinstance(r, dict)
                else None
                for r in results
            ]
            hit = recall_at_k(
                [{"id": i} for i in ids if i], pair["expected_source_ids"], args.k
            )
        except Exception as e:  # noqa: BLE001 - runner reports, never crashes
            print(f"ERROR  {pair['question'][:60]!r}: {e}")
            hit = 0.0
        outcomes.append({"question": pair["question"], "hit": bool(hit)})
        print(f"{'HIT ' if hit else 'MISS'}  {pair['question'][:70]}")

    summary = summarize(outcomes)
    print(json.dumps(summary))
    return 0 if summary["recall"] >= 1.0 else 1


if __name__ == "__main__":
    main()
