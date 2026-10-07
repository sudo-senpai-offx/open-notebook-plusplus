#!/usr/bin/env python3
"""Latency probe suite for the open-notebook REST API (Phase 0, perf audit).

Measures request latency for the flows that make a self-hosted low-end install
feel fast or slow (ADR-009, docs/7-DEVELOPMENT/perf-audit.md budgets):

  health              GET  /health
  notebooks.list      GET  /api/notebooks
  notebooks.load      GET  /api/notebooks/{id}
  sources.list        GET  /api/sources?notebook_id=
  search.text.scoped  POST /api/search {type: text, notebook_id}
  search.vector       POST /api/search {type: vector, notebook_id}   (bge-m3 via Ollama)
  search.vector.decomposed  POST /api/search + X-OpenNotebook-Timings:1 ->
                      also reports the server-side embed/db/assemble split headers
                      (A1 step 0; requires the baked-in deployment) underneath the
                      total line (as .embed/.db/.assemble rows)
  search.text.global  POST /api/search {type: text}                  (whole KB)
  ingest.text         POST /api/sources/json {type: text}  -> create probe notebook+source, then delete (--flows ingest)

Each flow is hit N times (default 5) and reported as p50/p90/mean/min/max ms.
The probe notebook is auto-discovered: the non-measure-*/probe-* notebook with
the most sources (heaviest realistic corpus). Override with --notebook-id.

Usage:
  scripts/latency_probes.py [--base URL] [-n N] [--flows FLOWS] [--mode NAME]
                            [--notebook-id ID] [--json] [--timeout S]

  --base       API base URL (default http://localhost:5055)
  -n/--iter    iterations per flow (default 5, >=2 so a warmup beat is dropped)
  --flows      comma-separated subsets: all (default), ingest
  --mode       label for the report (normal|throttled) - metadata only
  --notebook-id  force a specific probe notebook (skips auto-discovery)
  --json       machine-readable JSON report (also written to the report path)
  --timeout    per-request timeout seconds (default 120; vector/global are slow)

Exit code 0 = all flows probed; 2 = hard setup failure (API unreachable).
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Tuple


def _request(
    base: str,
    method: str,
    path: str,
    body: Dict[str, Any] | None = None,
    timeout: int = 120,
    headers: Dict[str, str] | None = None,
) -> Tuple[int, Dict[str, Any], Dict[str, str]]:
    """Core request; always returns (status, payload, response_headers)."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode()
            try:
                payload = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                payload = {"_raw": raw[:200]}
            return resp.status, payload, dict(resp.headers.items())
    except urllib.error.HTTPError as e:
        raw = e.read().decode(errors="replace")[:200]
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = {"_raw": raw}
        return e.code, payload, {}


def http(
    base: str,
    method: str,
    path: str,
    body: Dict[str, Any] | None = None,
    timeout: int = 120,
    headers: Dict[str, str] | None = None,
) -> Tuple[int, Dict[str, Any]]:
    status, payload, _ = _request(
        base, method, path, body=body, timeout=timeout, headers=headers
    )
    return status, payload


def http_with_headers(
    base: str,
    method: str,
    path: str,
    body: Dict[str, Any] | None = None,
    timeout: int = 120,
    headers: Dict[str, str] | None = None,
) -> Tuple[int, Dict[str, Any], Dict[str, str]]:
    return _request(base, method, path, body=body, timeout=timeout, headers=headers)


def time_http(*args: Any, **kwargs: Any) -> Tuple[float, int, Dict[str, Any]]:
    start = time.perf_counter()
    status, payload = http(*args, **kwargs)
    return (time.perf_counter() - start) * 1000.0, status, payload


def time_http_with_headers(
    *args: Any, **kwargs: Any
) -> Tuple[float, int, Dict[str, Any], Dict[str, str]]:
    start = time.perf_counter()
    status, payload, headers = http_with_headers(*args, **kwargs)
    return (time.perf_counter() - start) * 1000.0, status, payload, headers


# Server-side phase split for the vector flow (A1 step 0), emitted only when
# the deployment has the baked-in code. Header names are matched
# case-insensitively: h11/uvicorn transmit them lowercased on the wire.
TIMING_HEADER_NAMES = (
    "X-Notebook-Embed-Ms",
    "X-Notebook-Db-Ms",
    "X-Notebook-Assemble-Ms",
)


def header_lookup(headers: Dict[str, str], name: str) -> str | None:
    target = name.lower()
    for key, value in headers.items():
        if key.lower() == target:
            return value
    return None


def decomposed_vector_hit(
    base: str,
    notebook_id: str,
    timeout: int,
    sub_beats: Dict[str, List[float]],
) -> Tuple[float, int, Dict[str, Any]]:
    """Timed vector hit that also records the server-side phase split.

    Sends ``X-OpenNotebook-Timings: 1``; the baked-in API responds with the
    embed/db/assemble split as X-Notebook-* headers (A1 step 0). When the
    deployment lacks the code, or the vector model is unavailable, the
    headers simply don't appear and sub_beats stays empty.
    """
    total_ms, status, payload, headers = time_http_with_headers(
        base,
        "POST",
        "/api/search",
        {"query": "the importance", "type": "vector", "notebook_id": notebook_id},
        timeout=timeout,
        headers={"X-OpenNotebook-Timings": "1"},
    )
    if status < 400:
        for name in TIMING_HEADER_NAMES:
            value = header_lookup(headers, name)
            if value is not None:
                try:
                    sub_beats.setdefault(name, []).append(float(value))
                except ValueError:
                    pass  # ignore garbage header values; treat as absent
    return total_ms, status, payload


def _make_decomposed_hit(
    base: str, notebook_id: str, timeout: int, sub_beats: Dict[str, List[float]]
) -> Any:
    """Closure factory for the decomposed flow.

    The probe loop drops its first (warmup) beat from the totals; drop the
    first call's splits too so the decomposition rows cover the same beats.
    """
    calls = {"n": 0}

    def hit() -> Tuple[float, int, Dict[str, Any]]:
        total_ms, status, payload = decomposed_vector_hit(
            base, notebook_id, timeout, sub_beats
        )
        calls["n"] += 1
        if calls["n"] == 1:
            sub_beats.clear()
        return total_ms, status, payload

    return hit


def discover_probe_notebook(base: str, timeout: int) -> str:
    status, payload = http(base, "GET", "/api/notebooks", timeout=timeout)
    if status >= 400:
        raise RuntimeError(f"GET /api/notebooks failed: {status} {payload}")
    # GET /api/notebooks returns a JSON list of notebook objects.
    notebooks: List[Dict[str, Any]] = payload if isinstance(payload, list) else []
    candidates = [
        nb
        for nb in notebooks
        if not nb.get("name", "").startswith(("measure-", "probe-", "fixture-"))
        and not nb.get("archived", False)
    ]
    if not candidates:
        raise RuntimeError("no usable probe notebook found (all archived/measure-*)")
    candidates.sort(key=lambda nb: nb.get("source_count", 0), reverse=True)
    return candidates[0]["id"]


def build_flows(
    base: str,
    iter_n: int,
    notebook_id: str,
    timeout: int,
    flows_arg: str,
) -> List[Dict[str, Any]]:
    """Return the ordered flow spec list. Each spec yields timed beats.

    ``flows_arg`` selects the flows to run: "all" (default), or a
    comma-separated list of flow names and/or the "ingest" group.
    """
    decomposed_sub: Dict[str, List[float]] = {}
    read_flows: Dict[str, Dict[str, Any]] = {
        "health": {
            "name": "health",
            "iterations": iter_n,
            "hits": lambda: time_http(base, "GET", "/health", timeout=timeout),
        },
        "notebooks.list": {
            "name": "notebooks.list",
            "iterations": iter_n,
            "hits": lambda: time_http(base, "GET", "/api/notebooks", timeout=timeout),
        },
        "notebooks.load": {
            "name": "notebooks.load",
            "iterations": iter_n,
            "hits": lambda: time_http(
                base, "GET", f"/api/notebooks/{notebook_id}", timeout=timeout
            ),
        },
        "sources.list": {
            "name": "sources.list",
            "iterations": iter_n,
            "hits": lambda: time_http(
                base, "GET", f"/api/sources?notebook_id={notebook_id}", timeout=timeout
            ),
        },
        "search.text.scoped": {
            "name": "search.text.scoped",
            "iterations": iter_n,
            "hits": lambda: time_http(
                base,
                "POST",
                "/api/search",
                {"query": "the importance", "type": "text", "notebook_id": notebook_id},
                timeout=timeout,
            ),
        },
        "search.vector": {
            "name": "search.vector",
            "iterations": iter_n,
            "hits": lambda: time_http(
                base,
                "POST",
                "/api/search",
                {
                    "query": "the importance",
                    "type": "vector",
                    "notebook_id": notebook_id,
                },
                timeout=timeout,
            ),
        },
        "search.vector.decomposed": {
            "name": "search.vector.decomposed",
            "iterations": iter_n,
            "sub_beats": decomposed_sub,
            "hits": _make_decomposed_hit(base, notebook_id, timeout, decomposed_sub),
        },
        "search.text.global": {
            "name": "search.text.global",
            "iterations": iter_n,
            "hits": lambda: time_http(
                base,
                "POST",
                "/api/search",
                {"query": "the importance", "type": "text"},
                timeout=timeout,
            ),
        },
        "ingest.text": {
            "name": "ingest.text",
            "iterations": iter_n,
            "hits": lambda: ingest_probe(base, timeout=timeout),
        },
    }

    def select(names: List[str]) -> List[Dict[str, Any]]:
        # "ingest" is a group alias for the single ingest.text flow.
        expanded: List[str] = []
        for name in names:
            if name == "ingest":
                expanded.append("ingest.text")
            else:
                expanded.append(name)
        return [read_flows[name] for name in expanded if name in read_flows]

    if flows_arg == "all":
        return list(read_flows.values())
    return select([token.strip() for token in flows_arg.split(",")])


def ingest_probe(base: str, timeout: int) -> Tuple[float, int, Dict[str, Any]]:
    """Create a probe-* notebook, POST a text source into it, then delete both.

    Measures end-to-end text ingest latency for a small source. Uses a
    notebook/source pair named probe-* (deleted afterwards, matching the
    measure_resources.sh probe convention). Cleans up even on mid-flow failure.
    """
    import uuid

    tag = f"probe-{int(time.time() * 1000)}-{uuid.uuid4().hex[:6]}"
    notebook_name = f"{tag}-nb"
    nb_id: str | None = None
    payload: Dict[str, Any] = {
        "name": notebook_name,
        "description": "latency probe (auto-deleted)",
    }
    try:
        # Warmup beat (DNS + connection setup + import warm path) is dropped by
        # the caller; the first real beat is the timed one.
        status, created = http(base, "POST", "/api/notebooks", payload, timeout=timeout)
        if status >= 400:
            return 0.0, status, created
        nb_id = created["id"]
        source_body = {
            "type": "text",
            "notebooks": [nb_id],
            "title": f"{tag}-source",
            "content": (
                "Latency probe source. " * 40
            ),  # ~1KB of text, trivial to chunk/embed
            "embed": False,
            "async_processing": False,
        }
        start = time.perf_counter()
        # POST /api/sources is multipart-form-only (Depends(parse_source_form_data)),
        # so a JSON body gets a 422. /sources/json accepts SourceCreate directly.
        status, created_source = http(
            base, "POST", "/api/sources/json", source_body, timeout=timeout
        )
        return (time.perf_counter() - start) * 1000.0, status, created_source
    finally:
        # Best-effort cleanup.
        if nb_id:
            http(base, "DELETE", f"/api/notebooks/{nb_id}", timeout=timeout)


def percentile(sorted_ms: List[float], pct: float) -> float:
    """Nearest-rank percentile from a sorted list of ms values."""
    if not sorted_ms:
        return 0.0
    rank = max(0, min(len(sorted_ms) - 1, int(len(sorted_ms) * pct) - 1))
    return sorted_ms[rank]


def summarize(ms_list: List[float]) -> Dict[str, float]:
    """p50/p90/mean/min/max from a list of per-beat ms values."""
    if not ms_list:
        return {
            "mean_ms": 0.0,
            "p50_ms": 0.0,
            "p90_ms": 0.0,
            "min_ms": 0.0,
            "max_ms": 0.0,
        }
    sorted_ms = sorted(ms_list)
    return {
        "mean_ms": round(statistics.mean(ms_list), 1),
        "p50_ms": round(percentile(sorted_ms, 0.5), 1),
        "p90_ms": round(percentile(sorted_ms, 0.9), 1),
        "min_ms": round(min(ms_list), 1),
        "max_ms": round(max(ms_list), 1),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base", default="http://localhost:5055")
    ap.add_argument("-n", "--iter", type=int, default=5)
    ap.add_argument("--flows", default="all", help="all (default) or ingest")
    ap.add_argument("--mode", default="", help="report label, e.g. normal/throttled")
    ap.add_argument("--notebook-id", default=None, help="skip auto-discovery")
    ap.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    ap.add_argument("--timeout", type=int, default=120)
    args = ap.parse_args()

    if args.iter < 2:
        print("error: -n must be >= 2 (first beat is a warmup)", file=sys.stderr)
        return 2

    try:
        notebook_id = args.notebook_id or discover_probe_notebook(
            args.base, args.timeout
        )
    except Exception as e:  # noqa: BLE001 - setup failure path
        print(f"error: {e}", file=sys.stderr)
        return 2

    flows = build_flows(args.base, args.iter, notebook_id, args.timeout, args.flows)

    report: Dict[str, Any] = {
        "base": args.base,
        "mode": args.mode,
        "probe_notebook_id": notebook_id,
        "iterations": args.iter,
        "flows": {},
    }

    print(
        f"base={args.base} mode={args.mode or 'default'} probe_notebook={notebook_id}"
    )
    header = (
        f"{'flow':<22}{'n':>3}{'mean_ms':>9}{'p50':>9}{'p90':>9}{'min':>9}{'max':>9}"
    )
    print(header)
    print("-" * len(header))

    for flow in flows:
        name = flow["name"]
        beats_ms: List[float] = []
        errors: List[str] = []
        for i in range(flow["iterations"]):
            try:
                ms, status, payload = flow["hits"]()
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                errors.append(str(e))
                continue
            if status >= 400:
                errors.append(f"HTTP {status}: {str(payload)[:120]}")
                continue
            if i > 0:  # skip the warmup beat
                beats_ms.append(ms)
        summary = summarize(beats_ms)
        report["flows"][name] = {**summary, "errors": errors}
        status_mark = "OK"
        if errors:
            status_mark = f"ERR {errors[0][:40]}"
        if beats_ms:
            print(
                f"{name:<22}{len(beats_ms):>3}{summary['mean_ms']:>9.1f}"
                f"{summary['p50_ms']:>9.1f}{summary['p90_ms']:>9.1f}"
                f"{summary['min_ms']:>9.1f}{summary['max_ms']:>9.1f}  {status_mark}"
            )
        else:
            print(
                f"{name:<22}{0:>3}{'-':>9}{'-':>9}{'-':>9}{'-':>9}{'-':>9}  {status_mark}"
            )

        # A1 step 0: server-side phase split rows (only for flows that collected
        # them, i.e. the decomposed vector flow against a baked-in deployment).
        for sub_label, header_name in (
            (".embed", "X-Notebook-Embed-Ms"),
            (".db", "X-Notebook-Db-Ms"),
            (".assemble", "X-Notebook-Assemble-Ms"),
        ):
            sub_vals = flow.get("sub_beats", {}).get(header_name, [])
            if not sub_vals:
                continue
            sub_summary = summarize(sub_vals)
            report["flows"][name].setdefault("decomposed", {})[header_name] = (
                sub_summary
            )
            print(
                f"{sub_label:<22}{len(sub_vals):>3}{sub_summary['mean_ms']:>9.1f}"
                f"{sub_summary['p50_ms']:>9.1f}{sub_summary['p90_ms']:>9.1f}"
                f"{sub_summary['min_ms']:>9.1f}{sub_summary['max_ms']:>9.1f}"
            )

    print("-" * len(header))
    if args.json:
        print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
