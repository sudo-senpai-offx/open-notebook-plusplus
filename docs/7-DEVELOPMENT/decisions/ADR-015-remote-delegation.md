# ADR-015: Cross-device work is remote delegation, not a distributed queue

- **Status**: Accepted
- **Date**: 2026-10-16
- **Supersedes**: the decision recorded in `api/routers/processing.py` and
  §30 of `research-sync.md` that a cross-device executor was declined
- **Related**: ADR-004 (background workers), ADR-014 (RAM-only runtime)

## Context

A cross-device executor was previously declined for stated reasons: a shared
queue would need live coordination between databases that are separate by
design, per-job byte transfers, result merging through a path narrower than the
manifest, and — decisively — a phone-generated insight would land in the same
library as PC-generated ones. That last objection was the load-bearing one, and
it is correct.

The requirement has since changed: cross-device processing is wanted, and
configurable. So the question became whether the objection is fatal or whether a
different mechanism avoids it.

## Decision

Cross-device work is **remote delegation**. A device that cannot fit a job asks a
reachable peer to run it, over the existing HTTP API, authenticated with
per-device credentials.

The phone is the client, not the generator. The PC generates the insight with its
own model, and the result syncs back through the channel that already exists.

Delegation mode is a per-device policy: `never` (host-only default),
`when-peer-available`, `prefer-faster`.

## Why this and not the declined design

- **No cross-database coordination.** The phone does not join the PC's queue. It
  makes a request. Each database stays exactly as separate as the offline-first
  design intends.
- **No device-flavoured content.** The objection that killed the original design
  does not apply, because generation happens on one device. The phone's 1.5b
  model never authors an insight.
- **It dissolves the memory problem instead of managing it.** Under ADR-014 a
  generation job that will not fit in phone RAM no longer has to wait — it
  delegates.
- **Division of labour falls out.** Extraction and OCR stay on the phone (large
  SoC, tesseract local, byte-identical whoever runs them); generation goes to
  whichever device can hold the model. Cross-device acceleration becomes default
  behaviour rather than a special mode.

## Consequences

- **Delegation must be idempotent.** The tunnel dies on every reconnect; a
  delegated job whose response never arrives must be safely re-delegatable, and
  must not produce a second insight. The queue-aware dedup key covers this.
- **Per-device credentials, revocable independently.** A host can direct other
  devices and assign their work. A shared token means any single compromised
  device can impersonate a host to the rest, so one revocation re-opening the
  whole cluster is not acceptable.
- **Peers must be able to refuse** — busy, out of memory, or deliberately strict.
  A device retrying a refusing peer is worse than one that simply holds the job.
- Delegation is a privileged operation and its endpoints are authenticated as
  such, reusing the sync token scheme rather than a parallel one.
- Latency becomes sync + generate + sync-back rather than generate. Acceptable
  for batch; interactive work stays local unless explicitly delegated.

## Alternatives considered

- **Distributed queue over the sync channel.** The originally declined design.
  Coordination across databases that are offline-first by construction, plus the
  content-mixing objection. Both still apply.
- **Phone generates, results are stamped with provenance.** Preserves direct
  execution but still mixes 1.5b output into a shared library, merely labelling
  it. Labelling is not a substitute for not doing it.
- **No cross-device work at all.** Rejected: it wastes an idle 8 GB SoC and
  leaves the phone's memory constraint with no exit.