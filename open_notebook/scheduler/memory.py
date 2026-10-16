"""Free-memory reading for the admission gate.

Why this reads /proc/meminfo by hand
------------------------------------
`psutil` would be the obvious choice and it is the wrong one here. It is not
a dependency of this project, not installed, and the single place that touches
it (`open_notebook/ai/capabilities.py`) imports it inside a `try/except` and
carries on without it - which is the pattern for an *optional* library, not a
present one. A review of the admission design took the optional import at face
value and concluded memory reads were free; they are not. Six lines of parsing
removes the question.

Why `MemAvailable` is not trusted alone
---------------------------------------
`MemAvailable` is an estimate of what the kernel can hand a new allocation
*without swapping*. On a normal Linux box that is close to what a process can
fault in. On the devices this targets it is optimistic in a specific and
dangerous way: it counts **reclaimable page cache**, and the page cache is
exactly where model blobs and SurrealDB's RocksDB write buffers live. A phone
can report several gigabytes "available" while a process asking for that memory
would be pushed into reclaim, then stall behind the reclaim.

So the gate gets two numbers, not one:

* `available` - what the kernel says, used for the reserve check.
* `total` - used for the ceiling check, which is the one that cannot be
  inflated by page cache, because it asks "would admitting this push the
  device past a fraction of all its memory", not "is there memory spare".

`swapped` is also reported, because under ADR-014 the runtime is RAM-only and a
non-zero value means something has already gone wrong. It is surfaced rather
than acted on: the gate's job is to avoid causing an OOM, and reading swap is how
an operator finds out the OOM happened anyway.

Reads are deliberately dependency-free and allocation-light - this runs on
every dispatcher pass, inside proot, on the lowest tier of hardware.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

MEMINFO = Path("/proc/meminfo")

#: Only the fields the gate reasons about. `MemFree` is read too because it is
#: the difference between `MemAvailable` being a real estimate and being a
#: page-cache fiction - a large gap means the estimate is mostly cache.
_WANTED = ("MemTotal", "MemAvailable", "MemFree", "SwapTotal", "SwapFree")


@dataclass(frozen=True)
class MemoryReading:
    """A point-in-time view, in bytes.

    `total` is 0 when /proc/meminfo could not be read at all. Callers must
    treat that as "unknown", not as "zero available": a gate that reads an
    unreadable file as zero free memory holds every job forever, and a gate
    that reads it as unlimited promotes everything. Both are silent wedges, so
    `readable` exists to let a caller choose the failure direction explicitly.
    """

    total: int = 0
    available: int = 0
    free: int = 0
    swapped: int = 0
    readable: bool = False

    @property
    def available_or_none(self) -> int | None:
        return self.available if self.readable else None

    @property
    def cache_inflated(self) -> bool:
        """True when `MemAvailable` is mostly reclaimable cache.

        A gap this large means the number the gate is about to trust is
        substantially page cache rather than genuinely free memory. The gate
        uses the ceiling check in preference to the reserve check when this is
        true, because the ceiling cannot be inflated by cache.
        """
        if not self.readable or self.available <= 0:
            return False
        return (self.available - self.free) > self.available // 2


_LINE = re.compile(r"^(\w+):\s+(\d+)\s*kB$")


def read_memory(path: Path | None = None) -> MemoryReading:
    """Parse /proc/meminfo. Never raises.

    The dispatcher runs this every pass, on a device that may be under memory
    pressure itself. An exception here would take the scheduler down and stop
    all admission - so a failed read is a value the caller can reason about,
    not an error it has to catch.
    """
    target = path or MEMINFO
    try:
        text = target.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return MemoryReading()

    kilobytes: dict[str, int] = {}
    for line in text.splitlines():
        match = _LINE.match(line)
        if match and match.group(1) in _WANTED:
            kilobytes[match.group(1)] = int(match.group(2))

    if "MemTotal" not in kilobytes:
        return MemoryReading()

    return MemoryReading(
        total=kilobytes["MemTotal"] * 1024,
        available=kilobytes.get("MemAvailable", 0) * 1024,
        free=kilobytes.get("MemFree", 0) * 1024,
        swapped=(kilobytes.get("SwapTotal", 0) - kilobytes.get("SwapFree", 0)) * 1024,
        readable=True,
    )


def resolve_memory_path() -> Path | None:
    """Where to read on this device, or None if nowhere plausible.

    proot does not virtualise /proc/meminfo - it passes the host's through -
    so the default path is correct on the phone and this exists only so tests
    and any future sandbox can point somewhere else.
    """
    if MEMINFO.exists():
        return MEMINFO
    return None


def current_memory() -> MemoryReading:
    """The reading for this pass.

    Deliberately uncached. An earlier version cached this and the cache had no
    expiry, which meant the gate could admit or hold work against a memory
    state from arbitrarily long ago - and the failure is silent, because a
    stale reading still looks like a plausible one. Once per pass is cheap
    enough that correctness wins: /proc/meminfo is a kernel-generated file and
    the dispatcher reads it at loop cadence, not per promotion.
    """
    return read_memory(resolve_memory_path())
