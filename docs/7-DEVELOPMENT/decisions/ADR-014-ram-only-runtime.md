# ADR-014: The runtime is RAM-only and the app polices itself

- **Status**: Accepted
- **Date**: 2026-10-16
- **Related**: ADR-009 (low-end x86 optimisation), ADR-004 (background workers)

## Context

A measurement session on the PC found `AnonPages` at 3.2 GB out of 15.3 GB
reported, with the box simultaneously doing 1.7 GB/s of swap traffic at 1% idle
and 67% system time. `vm.swappiness` was 133 and zram0 held 14.8 GB of its 15 GB
backing store.

zram's backing store is RAM. Fourteen gigabytes of the machine was RAM holding
compressed pages that were themselves swapped out; new allocations then faulted
pages back in to be swapped out again. Ten insight generations took roughly two
hours, because a 5 GB model with 525 MB available does not run — it pages.

The working set was never the problem. The paging was.

The project's stated thesis is a learning OS that runs on low-end hardware. A
design that depends on the kernel absorbing memory pressure is a design that
quietly assumes RAM nobody on the floor class has.

## Decision

The runtime is RAM-only. Swap is not used, and the application enforces memory
discipline itself rather than relying on the OS to absorb scarcity.

When a job will not fit in free memory, it is **held** — not started, not
shrunk, not failed.

Explicitly rejected:

- **Keeping zram.** A compressed-RAM swap device competes for the exact pages it
  exists to protect when the working set is itself memory-hungry.
- **Disabling swap without gating.** Converts slow degradation into instant
  unclean OOM kills, most likely killing the model server mid-transformation and
  stranding the job as a stale failure.
- **Auto-switching to a smaller model under pressure.** A 1.5b summary generated
  on the phone syncing beside a 3b one on the PC is device-flavoured content in a
  single shared library. Quality must vary by device class only if the user
  chose it, never silently because memory was short.

## Consequences

- Memory headroom becomes a first-class runtime concern with a per-command-kind
  footprint table, a configurable reserve, and a recorded reason on every held
  job.
- A job can be legitimately stuck. The UI must distinguish "held on memory" from
  "no free slot", because they need different fixes.
- Peak swap becomes a pass/fail assertion of zero in testing, not a metric to
  optimise.
- The 3 GB floor class may not be able to run generation at all. That must be
  detected and reported rather than discovered as an OOM.
- Memory scarcity has a way out other than waiting: remote delegation to a peer
  (ADR-015).

## Alternatives considered

- **zram with low swappiness and an OOM score on ollama.** Least disruptive and
  fully reversible, but leaves a 15 GB RAM-consuming device installed on a box
  whose working set is 3 GB. Treats the symptom.
- **Disk-only swap, small.** Safer than no swap against hard OOM, but still makes
  every token generation touch a page fault. The project targets hardware where
  that is the difference between working and not.