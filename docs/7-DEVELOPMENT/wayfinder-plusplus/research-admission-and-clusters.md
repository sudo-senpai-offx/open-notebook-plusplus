# Admission control, RAM-only runtime, and multi-device work

- **Status**: Design agreed, not yet implemented
- **Date**: 2026-10-16
- **Supersedes**: the restart-based throttle shipped in `beef791` (see *What this reverses*)
- **Relates**: ADR-004 (background workers), ADR-009 (low-end x86 optimisation)

---

## Understanding

**What we're building**

1. A RAM-only runtime invariant, enforced by the app rather than by the OS.
2. Admission control: a job stays `held` until something admits it.
3. A memory gate: a job that will not fit in free memory is not promoted.
4. Hot full throttle: applies on the spot, never on restart.
5. Remote delegation: a peer device executes jobs this device cannot.
6. Device governance: host is a role, recomputed, with failover.
7. Source-processing visibility: summary strip plus attention list.

**Why**

A measurement session on the PC found the real constraint. Reported memory was
15.3 GB, of which only **3.2 GB was actual process memory** (`AnonPages`), with
llama-server holding 2.7 GB and nothing else above 150 MB. Simultaneously the
box ran **1.7 GB/s of swap traffic** (156k pages in, 285k out per second) at
**1% idle and 67% system time**. `vm.swappiness` was 133; zram0 held 14.8 GB of
its 15 GB.

zram's backing store is RAM. Fourteen gigabytes of the machine was RAM holding
compressed pages that were themselves swapped out, so new allocations faulted
pages back in to be swapped out again. That is the spiral, and it is why ten
insights took roughly two hours: a 5 GB model with 525 MB available does not run,
it pages.

The working set was never the problem. The paging was.

**Who for**

One person, on a 12-core / 15 GB PC and an 8 GB SDM680 phone, with a 3 GB
SDM439 as the floor class and a 2 GB x86 floor from ADR-009.

**Constraints**

- RAM-only is a hard requirement, not a preference.
- Never auto-switch models: device-flavoured insights would corrupt one shared
  library. This also rules out shrinking the model to fit.
- Running work is never cancelled; throttle changes apply going forward.
- Every feature that exists today must keep working — podcast generation, ask,
  search, the knowledge base. Throughput work must not regress any of them.

**Non-goals**

- A distributed cross-device queue. Superseded by remote delegation (§4).
- Auto-degrading model quality under memory pressure.
- Extrapolated performance claims for hardware we do not own (§7, Tier 3).
- Changing the insight scope (Dense Summary + Key Insights).

---

## Assumptions

Proposed because the user did not set them. Each is correctable, and none of
them block starting work.

| # | Assumption | Why this value |
|---|---|---|
| A1 | ask: time-to-first-token ≤ 8 s on SDM680 | Roughly one generation on the PC today; the phone's 1.5b should beat it |
| A2 | search: ≤ 1.5 s on SDM680 | `bge-m3` embed of a short query |
| A3 | cold start → usable UI: ≤ 20 s on SDM680 | Excludes model load, which is counted separately |
| A4 | Dense Summary on a typical source: ≤ 10 min on PC | Current measured range is 5–9 min under no paging |
| A5 | peak swap events: **0**, asserted not optimised | RAM-only is an invariant |
| A6 | stack RSS excluding models: ≤ 700 MB | ADR-009 measured ~515 MB mid-task; the dispatcher adds headroom |
| A7 | reserve floor: ~15% of RAM or 1.5 GB, whichever is larger | Keeps the device usable while work runs |
| A8 | model footprint ≈ 1.2× bytes-on-disk | KV cache and activations; derived, not tabulated, so it ages with the model registry |

---

## Decision log

| Decision | Chosen | Rejected | Why |
|---|---|---|---|
| Paging posture | RAM-only, app-enforced | keep zram; disable swap without gating | zram competes for the pages it exists to protect |
| Memory scarcity | hold the queue | auto-switch model; report only | a smaller model yields device-flavoured content |
| Gate location | admission, before the worker sees a job | inside the job; at submit only | only admission can actually withhold |
| Hot throttle | live window, read each pass | restart; SIGTERM on shrink | cancel strands in-flight work |
| Shrink behaviour | apply going forward | cancel the excess | reclaiming RAM faster is worth less than not losing work |
| Off-device work | remote delegation | distributed queue | delegation sidesteps cross-database coordination and keeps generation on one device |
| Host assignment | most capable available | random; most-free-memory | same convergence as random, no landing on a device that cannot schedule |
| Source view | summary strip + attention list | trust only; attention only; aggregate first | answers "is it growing" and "what needs me" in one flow |
| Cross-device proof | Tier 1 + Tier 2 | Tier 3 extrapolation | an extrapolation that ages badly is worse than no claim |

---

## 1. Admission control

Job lifecycle gains one state:

```
held ──promote──► new ──claim──► running ──► completed / failed / canceled
```

Submission writes **`held`**, never `new`. Nothing is claimable until the
dispatcher says so. This single change is what makes the memory gate and hot
throttle possible.

**The dispatcher** runs per device as a small loop, re-reading each pass:

1. Read the live throttle target — the promotion window size.
2. Read `MemAvailable`.
3. Count jobs currently in `new` **or** `running`.
4. Promote held jobs oldest-first while `in_flight < window` and memory permits.
5. Reap `running` jobs whose heartbeat has gone stale, returning them to `held`.

The window counts `new` + `running` together. The vendor worker flips
`new`→`running` asynchronously, so a burst of promotions would otherwise sit in
`new` and bind its semaphore behind our back.

**The vendor worker is unchanged.** Its `--max-tasks` becomes a deliberately high
backstop that should never bind, because the dispatcher is the real limit.

**Why submission cannot use `submit_command`**: that helper hardcodes
`status: "new"`, and the worker's live subscription can claim a row before we
flip it afterwards. Jobs are written directly with `status: "held"`, validating
arguments against the same command registry first. That is an explicit small
function rather than a race.

---

## 2. The memory gate

**Footprint is per command kind**, not per model alone — treating every job as a
full LLM load would stall cheap work behind a memory hold for no reason.

| Kind | Footprint |
|---|---|
| text extraction, chunking | ~64 MB |
| embedding (`embed_source`, `embed_insight`) | embedding model ≈ 1.2 GB |
| OCR / PDF image path | tesseract, device-dependent |
| generation (`run_transformation`) | generation model, derived from disk size |

**Rule**: promote when `MemAvailable − job_footprint ≥ reserve`.

Sources: `psutil` for `MemAvailable`; Ollama's model list for bytes on disk. On
proot, `/proc/meminfo` passes through to the host, so the phone reads real device
memory — to be confirmed on hardware, but there is no reason to expect otherwise.

Deriving from disk size keeps this honest as models change, rather than a
hardcoded table that silently rots.

The reserve differs by device: the 8 GB phone can afford a real floor; the 3 GB
floor class may not be able to run generation at all, and pretending otherwise
moves the failure from a clean hold to an OOM kill.

**Held jobs record why** — `held_reason` of `memory` or `no-slot`. A job held on
memory is a different problem from a job held because nothing is free, and a job
waiting three minutes is a third.

The gate is a headroom check, not a guarantee: it cannot predict fragmentation
or an unrelated spike. The reserve exists for exactly that gap.

---

## 3. Job classes and preemption

Measured contention: **ask** needs the generation model (~6 GB resident),
**search** needs the embedding model (~1.2 GB), **podcasts** need the LLM for the
outline plus a TTS path, and batch insight generation wants the same generation
model three at a time. Left alone, three transformations occupy the window while
a hand-typed question queues behind them.

| Class | Examples | Admission |
|---|---|---|
| **interactive** | ask, source chat, search | always admitted, never gated, never counted against the window |
| **foreground** | podcast episode, manual regenerate, manual ensure | under the window, preempts batch when queued |
| **batch** | scheduled insight generation, bulk embed, extraction backfill | window-controlled and memory-gated |

Preemption does not mean killing. The window is sized so a slot always exists:
batch gets `window − reserve`, and `reserve` slots stay free for whatever is
initiated by hand.

Interactive latency stays at roughly *one* generation rather than *n* because
`OLLAMA_NUM_PARALLEL=2` already lets the model server serve a second request
while the first runs. That is what makes "never cancel running work" affordable
rather than a compromise.

**Model residency follows the same arithmetic.** Ollama holds `bge-m3` and
`qwen3:4b` simultaneously today, about 1.2 GB for the embedding model. Forcing
one at a time thrashes: every search evicts the generator, every generation
reloads the embedder. Keep both resident on the PC and the 8 GB phone; on the
3 GB floor class residency becomes a per-device-class policy.

---

## 4. Remote delegation (the hybrid API)

Same process, two modes, selected automatically.

**Alone (host).** Today's behaviour: local dispatcher, local worker, local models.

**Peered (shared).** When a peer is reachable, the dispatcher gains an
alternative to holding a job. Before promoting it asks the peer: *can you take
this?* If yes, delegate; if no, the job stays held and the reason is recorded.

This replaces the cross-device executor previously declined, and dissolves the
objection that killed it. It is **remote delegation, not a distributed queue**:
the phone makes an HTTP call to the PC's API, the PC generates with its own
model, and nothing coordinates two separate databases. Device-flavoured content
disappears because the phone is the client, not the generator — a 1.5b phone
never authors an insight.

Delegation mode becomes the policy axis: `never` (host-only default) /
`when-peer-available` / `prefer-faster`.

Division of labour falls out rather than being assigned: the phone keeps
extraction and OCR (big SoC, tesseract local, byte-identical whoever runs them),
generation goes to whichever device can hold the model.

**Three consequences the design owns:**

- **Delegation must be idempotent.** The tunnel dies on every reconnect; a
  delegated job whose response never arrives must be safely re-delegatable. The
  queue-aware dedup key from `beef791` now covers this.
- **Auth is per device**, reusing the sync token scheme. A host can direct other
  devices, so a shared token means one compromised device can impersonate a host
  to the rest. Revocation must be per device.
- **Peers can refuse** — busy, out of memory, or deliberately strict. A phone
  retrying a refusing peer is worse than one that simply holds the job.

---

## 5. Device governance

**The host is a role, not an identity.** Nothing stores "I am the host". Any
device computes it from the same membership set and reaches the same answer, so
failover needs no election protocol, no consensus log, and no state transfer.

**Host assignment on disconnect: most capable available device.** This needs no
protocol either, because every device already knows each peer's class from its
status heartbeat and can independently compute the answer — convergent, exactly
like random, but a failover never lands on a device too small to schedule
anything.

**Membership** comes from discovery plus pairing — mDNS on the LAN, since
wireless is preferred over adb. Each peer heartbeats its class (RAM, cores), its
load, free memory, and its delegation policy.

**Failover is cheap because job state is never transferred.** Every device's
queue is its own; the sync channel moves content, not work. A new host starts
coordinating from live heartbeats rather than inheriting a backlog.

**Pause/resume is per-device and local**, and doubles as a courtesy signal: a
paused device advertises "don't delegate to me". Being able to refuse is what
makes pause meaningful rather than advisory.

Single device: that device is trivially host. Multi-device: one connected
device is the floor for the shared mode.

---

## 6. Source processing UI

Extends the existing Processing panel — no new pages, no new navigation.

**Summary strip**, always visible:

```
16 sources · 10 done · 3 running · 2 delegated · 1 held on memory
[Full throttle ▸]  memory 8.2/15.3 GB · 1.5 GB reserve · this device is host
```

Carries the hot throttle toggle, memory headroom against the reserve, and
whether this device is host or peer plus its delegation mode.

**Attention list** below — only what needs action, each row naming a reason and
offering the matching action:

| Reason | Action offered |
|---|---|
| held on memory | wait, or delegate to peer |
| failed extraction | retry |
| no text layer | run OCR |
| stale job | requeue |
| refused by peer | retry here |
| queued behind N | show position, no action |

The discipline is the one that killed hot-switch theatre earlier: **no state is
displayed that is not real.** "Summarising" means genuinely inside a running job.
"Queued · 12 ahead" shows the real count rather than an indeterminate spinner —
and because the dispatcher knows why a job is held, "held on memory" is
distinguishable from "no free slot".

Delegated rows name the device running them, so cross-device acceleration is
visible rather than asserted. The panel stays quiet when there is nothing wrong.

---

## 7. Verification

**Tier 1 — provable now, on the SDM680.** Full workflow end to end: cold start,
search latency, time-to-first-token, insight completion, peak memory throughout.
Because RAM-only is an invariant, peak swap is a pass/fail assertion of zero,
not a number to optimise.

**Tier 2 — provable with effort, and the one that actually proves the claim.**
Install stock OpenNotebook in the same guest, same sources, same models, same
day. Two things, one device. Everything else is inference.

**Tier 3 — dropped.** Extrapolation to SDM439 and unspecified "Pentium" parts
will be stale the moment it is written, and a stale claim is worse than none.
Recorded here as a non-goal rather than a result. If that hardware ever becomes
available, the Tier 1 harness is reusable.

**"Flawlessly" becomes numbers**: zero swap, zero OOM kills, every latency inside
the thresholds in the assumptions table.

**Test strategy**, following the established pattern:

- **Dispatcher** — randomised property tests over promotion windows, memory gates
  and reap timing, mirroring `test_sync_convergence.py`. No sequence of memory
  readings may produce two live jobs for one pair; a dispatcher restart is a
  no-op.
- **Delegation** — kill the response mid-flight and assert exactly one insight.
  Idempotency tested where it is most likely to break.
- **Host election** — property test that any permutation of a membership set
  yields the same host on every device. Convergence is the whole safety argument
  for failover, so it is proven rather than assumed.
- **Hot throttle** — window changes take effect with no restart, and never cancel
  in-flight work.
- **UI** — every reason in §6 renders, and no indeterminate spinner appears where
  a real count exists.

---

## What this reverses

`beef791` shipped restart-based throttle. Under this design it is deleted, at the
user's instruction, not because it was wrong for what it promised:

- `restart_required`, `throttle_restart_pending`, `throttle_target`
- `throttle.running` and the worker's testimony file
- the persisted preference read at startup, in both launchers

Their tests go too. This is deliberate: a restart-based toggle cannot satisfy
"apply on the spot", and the live-window design makes the entire reconciliation
unnecessary. Recorded here so the deletion is archaeology, not a mystery.

---

## Open questions

1. **Is podcast TTS local or hosted?** If it pulls another model into RAM it is a
   fourth contender in §2 and needs an explicit footprint row. Not yet
   determined.
2. Does `psutil` inside proot report host memory as expected? Confirming on
   hardware before the gate depends on it.
3. Split-brain: two devices partitioned from each other may each believe they are
   host. Acceptable on a LAN with per-device credentials, or does this need a
   guard?