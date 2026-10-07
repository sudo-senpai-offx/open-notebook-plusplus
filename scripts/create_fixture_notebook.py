#!/usr/bin/env python3
"""Create the small numbered fixture notebook for throttled heavy flows (A2).

Measurement decision (docs/7-DEVELOPMENT/locked-plan-2026-10.md, A2): throttled
latency probes must not run against the 85-source corpus -- that one stays for
unthrottled runs and memory probes. This script provisions a disposable
``fixture-throttle-<ts>`` notebook with a small numbered set of embedded text
sources (default 4, --sources), so the throttled probe suite searches a
realistic-but-small corpus at ~constant cost.

Each source is posted via POST /api/sources/json with embed=True and
async_processing=False. The POST returns once process_source has submitted the
embed job (embedding itself runs in the background), so the response may still
report embedded_chunks=0; as a safety net the script polls GET /api/sources/{id}
until embedded_chunks > 0, bounded by --timeout. On success it prints:

  FIXTURE_NOTEBOOK_ID=<id>

for scripts/throttle.sh (or the operator) to feed back into the probe runner.

Source text is generated from a small built-in set of neutral research-style
paragraphs; each source is ~5 KB so it chunks into several 400-token chunks
(default OPEN_NOTEBOOK_CHUNK_SIZE), giving a small-but-multi-chunk corpus.

Cleanup is manual (documented): delete the notebook (and its sources) from the
UI or via DELETE /api/notebooks/{id} when the measurement cycle is over. On a
hard failure mid-run the script best-effort deletes the partial fixture itself
so retries start clean. The probe notebook auto-discovery in
scripts/latency_probes.py excludes fixture-* notebooks so the fixture never
becomes the default probe corpus.

Usage:
  scripts/create_fixture_notebook.py [--base URL] [--sources N] [--timeout S]
                                     [--create-timeout S]

  --base            API base URL (default http://localhost:5055)
  --sources         number of sources to create (default 4, clamped to 1..8)
  --timeout         total wall-clock budget in seconds for embed+process
                    (default 900)
  --create-timeout  per-request timeout in seconds for each source POST
                    (default 300; the sync ingest POST can stall server-side
                    on a slow host, so this is separate from --timeout)

Exit code 0 = fixture ready; 2 = hard setup failure (API unreachable, a source
failed to embed within the timeout, or the source POST timed out / errored).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Dict


def http(
    base: str,
    method: str,
    path: str,
    body: Dict[str, Any] | None = None,
    timeout: int = 120,
) -> tuple[int, Dict[str, Any]]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode()
            try:
                payload = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                payload = {"_raw": raw[:200]}
            return resp.status, payload
    except urllib.error.HTTPError as e:
        raw = e.read().decode(errors="replace")[:200]
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = {"_raw": raw}
        return e.code, payload


# Neutral, research-style paragraphs. Each source concatenates a few of these
# (~5 KB total) so chunking produces several chunks per source.
_SOURCE_CONTENT: Dict[str, str] = {
    "primer": """\
Distributed systems manage state across multiple processes that share no memory
and communicate only by passing messages. The core difficulty is that messages
can be delayed, reordered, duplicated, or lost, so a program cannot assume that
a remote operation completed just because a reply arrived. This is why
distributed consensus, replication, and exactly-once semantics remain active
areas of study: each abstraction layer hides some of the underlying nondeterminism
but pays for it with coordination overhead. Systems that tolerate partitions and
still make progress are said to be eventually consistent, a property that trades
immediate accuracy for availability. Designing with failure in mind, rather than
treating it as an edge case, is what separates production systems from prototypes.

The practical consequence is that distributed programming demands discipline
about timeouts, retries, idempotency, and idempotent retries. A request that is
re-sent must produce the same side effect as the original, or the system risks
duplicating work such as charging a customer twice or writing a message twice.
Leases and epoch numbers help detect stale leaders; fencing tokens prevent an
old process from corrupting shared state after a failover. Observability tools
such as distributed tracing help operators reason about paths through many
services, because a single user action may fan out into dozens of internal
calls. At heart, the field is about making the unpredictable feel predictable.
""",
    "retrieval": """\
Retrieval-augmented workflows combine a generative model with a searchable
corpus so answers can be grounded in evidence rather than produced from
memory alone. A query is embedded into a dense vector and compared against a
precomputed index of document chunks using approximate nearest-neighbor search.
The top results are then fed to the model as context, allowing it to cite
specific passages and to answer questions that change over time without
retraining. The quality of the result depends heavily on the quality of the
index: chunk boundaries, embedding model, and the distance metric all shift
which passages are considered relevant.

Retrieval quality is usually measured by recall at a cutoff, which answers a
practical question: whether the information needed to answer the query is among
the candidates presented to the model. Because recall is cheap to compute and
hard to game, it is the metric most teams optimize first. A small corpus can
be served with a brute-force scan on every query, but as the corpus grows the
index must be partitioned and pruned. Caching embedding vectors, reindexing in
the background, and monitoring the latency breakdown of embed, retrieve, and
ranking steps are the levers most commonly used to keep a retrieval service
both fast and correct.
""",
    "telemetry": """\
Telemetry is the practice of measuring a running system from the inside so that
operators can observe its behavior without restarting or instrumenting it after
the fact. The three canonical pillars are logs, metrics, and traces. Logs record
discrete events with timestamps and severity; metrics are numeric series that
can be aggregated, like request rate or latency percentile; traces capture the
causal chain of a single request as it travels across services. Each pillar is
good at answering different questions, and mature systems integrate all three so
an alert on a metric can be chased into the relevant trace and then into the
underlying log lines.

Latency measurement deserves special care. A single average hides the long tail
that users actually feel, so percentiles such as p50, p90, and p99 are the
standard vocabulary. Comparing measurements fairly requires the same probe to
hit the same endpoint with the same payload before and after a change, dropping
warmup beats and capturing the environment's load. Recording the wall-clock
request latency alongside server-side phase timings makes it possible to tell
whether a regression lives in the network, the model, or the database. Without
that decomposition, optimization work tends to guess at the bottleneck instead
of measuring it.
""",
    "queueing": """\
Queueing theory explains why systems that seem to have plenty of capacity can
still feel slow: work does not arrive evenly. When arrivals are bursty and
service times vary, tasks accumulate in a queue, and each queued task waits
longer than the time it takes to do its own work. Utilization, arrival rate,
and service time combine to drive the average wait, and the relationship is
nonlinear -- small increases in utilization cause large increases in latency
as the system approaches saturation. Adding a second worker can more than
halve the wait, while over-provisioning to meet the worst observed burst is
wasteful for the rest of the time.

For interactive systems, the practical takeaway is that headroom is a feature.
Contention for CPU, memory, and I/O all show up in queueing effects, and a
latency budget that is met at idle may be badly exceeded during a burst or
when neighbors share the same host. Throttling experiments deliberately cap
resources so that tail latency and queue build-up become visible before they
reach production. Because queueing effects amplify small degradations, a
regression in throughput can hide at low load and appear suddenly at scale,
which is why load, latency, and resource usage should be measured together
rather than in isolation.
""",
    "embedding": """\
Embedding models map text to fixed-size vectors such that semantically similar
passages sit close together in the vector space. The embedding task is
unsupervised from the application's point of view: the model is trained on
massive text collections with objectives that pull related passages together
and push unrelated ones apart. Different models trade dimensions, speed, and
quality; a multilingual model, for instance, may spend capacity aligning
languages that a monolingual deployment never needs. Choosing an embedding
model is therefore a measurement problem: on a representative set of queries,
teams measure whether the top retrieved passages actually contain the answer
rather than trusting a single similarity number.

Embedding computation is also a practical performance concern. Each query
requires one forward pass through the model, so the embedding latency adds
directly to every search call. On a small host, the embedding step often
dominates the total because the vector lookup itself is quick. Techniques such
as caching embeddings by query text, truncating long inputs, and reducing the
stored vector dimensionality can all cut that cost, but each one adds its own
complication, which is why the measurement must come first and the optimization
second.
""",
}

_BASE_PARAGRAPHS = [
    ("primer", _SOURCE_CONTENT["primer"]),
    ("retrieval", _SOURCE_CONTENT["retrieval"]),
    ("telemetry", _SOURCE_CONTENT["telemetry"]),
    ("queueing", _SOURCE_CONTENT["queueing"]),
    ("embedding", _SOURCE_CONTENT["embedding"]),
]


def _source_content(index: int, total: int) -> str:
    """Compose ~5 KB of text for source ``index`` of ``total``.

    Rotates through the built-in topics so every source is a multi-chunk,
    searchable document, and each is dominated by a different topic so the
    corpus is meaningfully searchable per-topology.
    """
    topic = _BASE_PARAGRAPHS[index % len(_BASE_PARAGRAPHS)][1]
    padding_topic = _BASE_PARAGRAPHS[(index + 1) % len(_BASE_PARAGRAPHS)][1]
    # Two paragraphs of topic + one of a neighboring topic keeps sources
    # distinct yet each multi-chunk.
    return topic + "\n\n" + topic + "\n\n" + padding_topic


def wait_until_embedded(base: str, source_id: str, deadline: float) -> tuple[bool, int]:
    """Poll GET /api/sources/{id} until embedded_chunks > 0 or deadline.

    Transient network failures on a poll are retryable: the embed job may
    still be landing server-side, so a single dropped poll must not fail the
    fixture.
    """
    last_chunks = 0
    while time.monotonic() < deadline:
        try:
            status, payload = http(base, "GET", f"/api/sources/{source_id}", timeout=15)
        except (urllib.error.URLError, TimeoutError, OSError):
            time.sleep(2)
            continue
        if status >= 400:
            last_chunks = -1
        else:
            last_chunks = int(payload.get("embedded_chunks") or 0)
            if last_chunks > 0:
                return True, last_chunks
        time.sleep(2)
    return False, last_chunks


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base", default="http://localhost:5055")
    ap.add_argument("--sources", type=int, default=4)
    ap.add_argument("--timeout", type=int, default=900)
    ap.add_argument(
        "--create-timeout",
        type=int,
        default=300,
        help="per-request timeout (s) for the source POST; the ingest POST can "
        "stall server-side on a slow host, so this is independent of the poll "
        "deadline (--timeout)",
    )
    args = ap.parse_args()

    n_sources = max(1, min(8, args.sources))
    tag = f"fixture-throttle-{int(time.time())}"
    notebook_name = f"{tag}-nb"

    print(f"base={args.base} sources={n_sources} timeout={args.timeout}s")
    try:
        status, created = http(
            args.base,
            "POST",
            "/api/notebooks",
            {
                "name": notebook_name,
                "description": "A2 throttled-probe fixture (auto-created)",
            },
            timeout=30,
        )
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        print(f"error: API unreachable: {e}", file=sys.stderr)
        return 2
    if status >= 400:
        print(
            f"error: notebook create failed: HTTP {status} {created}", file=sys.stderr
        )
        return 2
    nb_id = created.get("id")
    if not nb_id:
        print(f"error: notebook create returned no id: {created}", file=sys.stderr)
        return 2
    print(f"notebook={nb_id} ({notebook_name})")

    deadline = time.monotonic() + args.timeout
    source_ids: list[str] = []
    total_chunks = 0

    def abort(reason: str) -> int:
        """Report a failure and best-effort delete the partial fixture."""
        print(f"error: {reason}", file=sys.stderr)
        try:
            status, _ = http(args.base, "DELETE", f"/api/notebooks/{nb_id}", timeout=30)
            if status < 400:
                print(
                    f"  cleaned up partial fixture notebook {nb_id}",
                    file=sys.stderr,
                )
            else:
                print(
                    f"  warning: cleanup DELETE returned HTTP {status}",
                    file=sys.stderr,
                )
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            print(f"  warning: cleanup DELETE failed: {e}", file=sys.stderr)
        return 2

    for i in range(1, n_sources + 1):
        title = f"{tag}-source-{i:02d}"
        body: Dict[str, Any] = {
            "type": "text",
            "notebooks": [nb_id],
            "title": title,
            "content": _source_content(i, n_sources),
            "embed": True,
            "async_processing": False,
        }
        try:
            status, payload = http(
                args.base,
                "POST",
                "/api/sources/json",
                body,
                timeout=args.create_timeout,
            )
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            return abort(f"source {i} POST failed: {e}")
        if status >= 400:
            return abort(
                f"source {i} processing failed: HTTP {status} {str(payload)[:200]}"
            )
        sid = payload.get("id")
        if not sid:
            return abort(f"source {i} returned no id: {str(payload)[:200]}")
        chunks = int(payload.get("embedded_chunks") or 0)
        if chunks == 0:
            # Safety net: the POST may have returned before the embed job landed.
            ok, chunks = wait_until_embedded(args.base, sid, deadline)
            if not ok:
                return abort(
                    f"source {i} ({sid}) not embedded within timeout "
                    f"(embedded_chunks={chunks})"
                )
        source_ids.append(sid)
        total_chunks += chunks
        print(f"  source {i}/{n_sources}: {sid} embedded_chunks={chunks}")

    print(f"FIXTURE_NOTEBOOK_ID={nb_id}")
    print(
        f"fixture ready: notebook={nb_id} sources={len(source_ids)} chunks={total_chunks}"
    )
    print(f"  sources: {', '.join(source_ids)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
