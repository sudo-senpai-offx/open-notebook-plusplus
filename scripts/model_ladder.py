#!/usr/bin/env python3
"""H2 model ladder: latency x generation quality, on whatever host runs it.

    python scripts/model_ladder.py evals/generation_fixture.json
        --api http://127.0.0.1:8080/v1          # llama-server on this host
        --label qwen2.5-0.5b-q2_k               # the model currently loaded
        [--timeout 180]

Two numbers per candidate, deliberately kept separate:

  - Latency comes from llama-bench (pp/tg tok/s), NOT from this script.
    This script's wall-clock includes HTTP, queueing and the sampler, which
    is what a user feels but is not a property of the model.
  - Quality is must_contain coverage. Deterministic, no judge model, no
    API cost - so two candidates are compared on exactly the same basis.

The point is the PAIR. A model that scores higher but takes 4x longer may
still be the wrong default; only the curve decides that, and the curve
needs both axes measured on the same prompt set.

Deliberately does NOT touch the app's model registry - it talks to
llama-server directly, so a ladder run cannot leave the phone configured
for a model you rejected.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request


def normalise(text: str) -> str:
    """Lowercase, collapse whitespace, drop punctuation-adjacent noise.

    Comparison has to survive markdown, punctuation and casing without
    being so loose that any answer 'contains' the fact.
    """
    text = text.lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def coverage(answer: str, must_contain: list[str]) -> tuple[float, list[str]]:
    """Fraction of required facts present, plus the ones that were missing."""
    haystack = normalise(answer)
    missing = [fact for fact in must_contain if normalise(fact) not in haystack]
    if not must_contain:
        return 0.0, []
    return (len(must_contain) - len(missing)) / len(must_contain), missing


def ask(api: str, prompt: str, timeout: int) -> tuple[str, float, str | None]:
    payload = {
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
        "max_tokens": 256,
    }
    req = urllib.request.Request(
        f"{api.rstrip('/')}/chat/completions",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    started = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            body = json.loads(response.read().decode())
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        return "", time.monotonic() - started, str(exc)
    elapsed = time.monotonic() - started
    try:
        return body["choices"][0]["message"]["content"], elapsed, None
    except (KeyError, IndexError) as exc:
        return "", elapsed, f"unexpected response shape: {exc}"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("fixture")
    parser.add_argument("--api", default="http://127.0.0.1:8080/v1")
    parser.add_argument("--label", default="candidate")
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--only", default=None, help="run one prompt id")
    args = parser.parse_args()

    with open(args.fixture, encoding="utf-8") as handle:
        fixture = json.load(handle)

    prompts = [p for p in fixture.get("prompts", []) if not p.get("sample")]
    if args.only:
        prompts = [p for p in prompts if p.get("id") == args.only]
    if not prompts:
        print("no prompts in fixture")
        return 0

    print(f"model: {args.label}   api: {args.api}")
    print(f"{'id':<12} {'cov':>5} {'secs':>7}  missing")
    print("-" * 64)

    total_cov = 0.0
    total_secs = 0.0
    errors = 0
    rows = []
    for entry in prompts:
        answer, secs, error = ask(args.api, entry["prompt"], args.timeout)
        if error:
            errors += 1
            print(f"{entry['id']:<12} {'ERR':>5} {secs:>7.1f}  {error[:40]}")
            rows.append({"id": entry["id"], "coverage": 0.0, "seconds": secs, "missing": entry.get("must_contain", [])})
            continue
        cov, missing = coverage(answer, entry.get("must_contain", []))
        total_cov += cov
        total_secs += secs
        flag = ",".join(missing) if missing else "-"
        print(f"{entry['id']:<12} {cov:>5.2f} {secs:>7.1f}  {flag}")
        rows.append({"id": entry["id"], "coverage": cov, "seconds": secs, "missing": missing})

    n = len(prompts)
    mean_cov = total_cov / n
    mean_secs = total_secs / n
    print("-" * 64)
    print(f"{'MEAN':<12} {mean_cov:>5.2f} {mean_secs:>7.1f}")
    if errors:
        print(f"WARNING: {errors}/{n} prompts errored — mean coverage is understated")

    # Machine-readable so successive candidates can be diffed rather than
    # re-read off a terminal.
    out = {
        "label": args.label,
        "api": args.api,
        "prompts": n,
        "mean_coverage": round(mean_cov, 4),
        "mean_seconds": round(mean_secs, 2),
        "errors": errors,
        "rows": rows,
    }
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())