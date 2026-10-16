"""The admission decision: should this job start now?

Pure and synchronous on purpose. The reviewed design buried this rule inside a
loop that also talked to the database, which made it impossible to test the
part that actually matters - and the part that matters had two real bugs in it
(per-job rather than per-model cost, and no aggregate budget across a pass).
Both are invisible unless the decision is separable from the plumbing.

Two checks, and they are not redundant
--------------------------------------
**Window.** How many jobs may be in flight at once. A concurrency control, not a
memory control.

**Memory.** Whether admitting this job would push the device past a limit.

The memory check runs twice because the two failure modes are different:

* A *reserve* check against `MemAvailable` answers "is there room right now".
* A *ceiling* check against `MemTotal` answers "would this push the device past
  a fraction of all its memory".

`MemAvailable` counts reclaimable page cache, and on these devices that cache
holds model blobs and the database's write buffers - so it can read several
gigabytes higher than what a process can actually fault. The ceiling cannot be
inflated that way, because it does not consult availability at all. On a normal
desktop the reserve is the right check; on the floor class the ceiling is what
stops an OOM.

When memory cannot be read
-------------------------
`/proc/meminfo` unreadable is not treated as "no memory" (which would hold
every job forever, silently) and not as "unlimited" (which would promote
everything). The gate degrades to **one job at a time**. That still makes
progress on a device whose memory reporting is broken, while keeping the
exposure to a single job rather than a full window - and if that one job is
too large, its own failure is the signal, instead of an OOM that takes the
model server with it.

Scope
-----
This governs the **command queue**. Interactive work - ask, source chat,
search - does not go through the queue at all; it calls the model directly.
That is why the reviewed design's interactive/foreground/batch taxonomy is not
reproduced here: two of its three classes had no members in this path. Those
requests are governed by the reserve, not by this window.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Literal

from open_notebook.scheduler.footprint import (
    EXPLICIT_FOOTPRINTS,
    FootprintEstimate,
    JobFootprint,
)
from open_notebook.scheduler.memory import MemoryReading

#: Why a job was not admitted. Surfaced verbatim in the API and the UI, so
#: these strings are part of the contract - a rename is a UI change.
HeldReason = Literal[
    "ok",
    "window-full",
    "memory",
    "unknown-footprint",
    "memory-unreadable",
]


@dataclass(frozen=True)
class GateDecision:
    """One decision, with everything needed to explain it."""

    admit: bool
    reason: HeldReason
    #: Bytes this job would add beyond what in-flight jobs already require.
    #: Zero when the models are already resident, which is the common case
    #: for a second transformation on the same model.
    incremental: int
    #: Projected model bytes if admitted, for the log line and the tests.
    projected: int

    @property
    def holds(self) -> bool:
        return not self.admit


def incremental_cost(committed: FootprintEstimate, candidate: JobFootprint) -> int:
    """What this job adds on top of what is already resident.

    Zero when every model the job needs is already loaded, because a second
    transformation against a loaded model genuinely costs no additional
    resident memory. This is the single most important number in the module:
    it is the difference between charging per job (~18 GB for three jobs on a
    15 GB device) and per model (~5 GB), and getting it wrong in either
    direction is either an OOM or a queue that stops moving.
    """
    return sum(
        cost for key, cost in candidate.models.items() if key not in committed.models
    )


def default_reserve(total_bytes: int) -> int:
    """Memory held back so the device stays usable while work runs.

    A floor of 512 MB for the floor class and a fraction of total above it.
    Absolute floors matter because a percentage on a 3 GB device is under
    500 MB, which is not enough to keep the shell, the database and the UI
    alive while a model loads.
    """
    return max(512 * 1024 * 1024, int(total_bytes * 0.15))


#: What a job costs in *this* process's memory regardless of where its
#: models run: the command payload, the extracted text, the chunk list. The
#: `__no_model__` entry because that is precisely what it is - the cost of
#: doing the work as opposed to running a model for it.
LOCAL_OVERHEAD = EXPLICIT_FOOTPRINTS["__no_model__"]


def model_pool_bytes(memory: MemoryReading) -> int:
    """The memory pool the *models* live in, which is not always this one.

    This is the correction for a failure that only appears once the whole
    thing runs on real hardware. On the phone the models are inside the guest
    beside the worker, so `/proc/meminfo` is the right answer. On the PC the
    worker runs in a Docker container while Ollama runs on the host - and
    Docker Desktop virtualises `/proc`, so the container sees the VM's 3.56 GB
    rather than the host's 15 GB.

    Comparing a model that lives on the host against the VM's total made the
    ceiling permanently unreachable: a 5.2 GB model can never fit under
    3.56 GB x 0.85, so every generation job was held forever. The gate looked
    correct throughout - it reported `memory`, which was true of the number it
    was handed - while refusing all work.

    So the pool is explicit. Unset means "models are here", which is the
    phone's case and the right default for a native install. The PC sets it,
    because there it is demonstrably not.
    """
    override = os.getenv("OPEN_NOTEBOOK_MODEL_POOL_BYTES")
    if override:
        try:
            value = int(override)
        except ValueError:
            return memory.total
        if value > 0:
            return value
    return memory.total


def evaluate(
    *,
    window: int,
    in_flight: int,
    committed: FootprintEstimate,
    candidate: JobFootprint,
    memory: MemoryReading,
    reserve: int | None = None,
    ceiling_fraction: float = 0.85,
    model_pool: int | None = None,
) -> GateDecision:
    """Decide whether to admit `candidate` right now."""
    incremental = incremental_cost(committed, candidate)
    projected = committed.total + incremental

    if in_flight >= window:
        # Checked before memory: a full window is the cheaper, more common
        # reason, and reporting it keeps the UI honest about *why* a queue is
        # not draining. A job held on a full window and a job held on memory
        # need different fixes.
        return GateDecision(
            admit=False, reason="window-full", incremental=incremental, projected=0
        )

    if not memory.readable:
        # Degraded, not blocked. One at a time.
        return GateDecision(
            admit=in_flight == 0,
            reason="memory-unreadable",
            incremental=incremental,
            projected=projected,
        )

    if candidate.uncertain:
        # "I could not find out how big this is" is not "this is small".
        return GateDecision(
            admit=False,
            reason="unknown-footprint",
            incremental=incremental,
            projected=0,
        )

    effective_reserve = (
        reserve if reserve is not None else default_reserve(memory.total)
    )

    pool = model_pool if model_pool is not None else model_pool_bytes(memory)

    # When the models live in this same pool, the job's whole footprint is
    # charged against this pool's availability. When they live elsewhere - the
    # PC, where Ollama runs on the host and only the worker is in here - then
    # charging the model's bytes against the VM's free memory is the same
    # category of error one check later: it subtracts a quantity that is not
    # available here, and holds work that would in fact have started.
    local_charge = incremental if pool == memory.total else LOCAL_OVERHEAD
    if memory.available - local_charge < effective_reserve:
        return GateDecision(
            admit=False, reason="memory", incremental=incremental, projected=0
        )

    if projected > pool * ceiling_fraction:
        # Cannot be inflated by page cache, so it still bites when MemAvailable
        # is mostly cache and looks comfortable.
        return GateDecision(
            admit=False, reason="memory", incremental=incremental, projected=0
        )

    return GateDecision(
        admit=True, reason="ok", incremental=incremental, projected=projected
    )
