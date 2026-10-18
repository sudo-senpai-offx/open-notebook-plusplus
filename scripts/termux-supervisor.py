#!/data/data/com.termux/files/usr/bin/python
"""Residency supervisor - the one thing that decides which models are loaded.

Why this exists
---------------
The phone started a 1.2 GB chat model and a 54 MB embedder at boot and left
them there whether or not anything was generating. Measured on the device with
the stack nominally healthy: MemAvailable 2073 MB, and 2517 MB of 5119 MB swap
in use. The resident-set figures were the tell - llama-server read 1222380 kB
at launch and 3288 kB ten minutes later. That is not a model shrinking, it is a
model being paged out to zram, and a model with 3 MB resident is not a small
model, it is an absent one.

So residency has to be managed rather than assumed. ADR-014 says a job that
will not fit is *held*, never shrunk; this applies the same rule one level up,
in that a model that will not fit is not loaded at all.

What it owns
------------
Every start and stop of a model server. It implements none of them. It shells
out to `onb chat-up` and `onb-embed up`, which reuse the launchers' existing
start and stop paths, and it asks them for residency facts through a `probe`
verb rather than scanning /proc itself.

That is deliberate rather than cautious. A second implementation of "start the
chat server" living in this file would be free to disagree with the launcher
about which pid is the chat server - and :8080 and :8081 are the same
executable, so that disagreement is not hypothetical. It has already happened:
a name-based match once had `start_llama` kill the embedder it meant to replace,
and the two servers then exited about fifteen seconds apart, each logging a
clean shutdown.

Who it talks to
---------------
The guest API, over loopback. proot does not create a network namespace, so
127.0.0.1 inside the guest is the same loopback Termux sees - which is why the
model servers on :8080 and :8081 answer from both sides, and why binding to
loopback is the whole of the access control here.

Serialisation
-------------
Loads take one process-wide lock. Two roles loading at once is the same race as
two launchers racing, and llama-server binds with SO_REUSEPORT, so the failure
mode is several processes each holding the port with none of them necessarily
holding the model - observed on the device as five processes claiming :8081.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional

HOST = "127.0.0.1"
PORT = int(os.getenv("OPEN_NOTEBOOK_RESIDENCY_PORT", "8099"))
BIN = os.path.expanduser("~/bin")

#: Weight bytes -> resident bytes. The same factor the admission gate uses, so
#: a model the gate would admit is a model this will let load.
WEIGHT_OVERHEAD = 1.10

#: Held back from MemAvailable before any load. Covers the guest stack
#: (surreal + uvicorn + worker + next, measured at ~40 MB resident together) and
#: Android's own headroom, which is the part that gets killed when we take it.
DEFAULT_RESERVE_MB = 512

#: Seconds a role may sit unused before it is unloaded. Zero disables the
#: reaper entirely, which is what the PC wants: it has 15 GB and no reason to
#: pay a model load on every cold request.
DEFAULT_IDLE_SECONDS = 300

LOAD_TIMEOUT = float(os.getenv("OPEN_NOTEBOOK_RESIDENCY_LOAD_TIMEOUT", "45"))
PROBE_TIMEOUT = float(os.getenv("OPEN_NOTEBOOK_RESIDENCY_PROBE_TIMEOUT", "8"))


@dataclass(frozen=True)
class Role:
    """One model server the supervisor can load or unload."""

    name: str
    probe: List[str]
    load: List[str]
    unload: List[str]
    model_path: List[str]
    port: int
    #: Lower runs first when the guest asks for several at once. Chat is
    #: interactive and embedding is bulk, so chat outranks it.
    priority: int
    #: Jobs of this class that may run at once. Embedding is small and benefits
    #: from a little parallelism; chat generation is single-stream.
    max_concurrent_jobs: int
    #: Threads this class holds while a job of it runs, from `cpu_threads` in
    #: onb-cpu. Charged against the shared CPU budget by the guest's admission
    #: gate so CPU and memory are held alike.
    threads: int = 2


ROLES: Dict[str, Role] = {
    "chat": Role(
        name="chat",
        probe=["sh", f"{BIN}/onb", "chat-status"],
        load=["sh", f"{BIN}/onb", "chat-up"],
        unload=["sh", f"{BIN}/onb", "chat-down"],
        model_path=["sh", f"{BIN}/onb", "model-path"],
        port=8080,
        priority=10,
        max_concurrent_jobs=1,
        threads=2,
    ),
    "embed": Role(
        name="embed",
        probe=["sh", f"{BIN}/onb-embed", "probe"],
        load=["sh", f"{BIN}/onb-embed", "up"],
        unload=["sh", f"{BIN}/onb-embed", "down"],
        model_path=["sh", f"{BIN}/onb-embed", "model-path"],
        port=8081,
        priority=50,
        max_concurrent_jobs=2,
        threads=2,
    ),
}

KV = re.compile(r"(\w+)=(\S+)")


def run(argv: List[str], timeout: float) -> tuple[int, str, str]:
    """Run a command, never raising.

    A supervisor that dies because a probe returned non-zero would take down the
    only thing that can restart the models, so every failure is a value.
    """
    try:
        done = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return done.returncode, done.stdout, done.stderr
    except subprocess.TimeoutExpired:
        return 124, "", f"timed out after {timeout:g}s"
    except OSError as exc:
        return 127, "", str(exc)


def parse_kv(text: str) -> Dict[str, str]:
    """Parse the `key=value` line the probe verbs emit.

    Whole-line anchored per line so a stray line of log output cannot be read
    as residency state - a launcher that prints a warning before its answer
    must not have that warning parsed as `resident=0`.
    """
    found: Dict[str, str] = {}
    for line in text.splitlines():
        if not line.strip() or "=" not in line:
            continue
        if re.match(r"^\[[a-z]", line.strip()):
            continue  # a bracketed log prefix, not our answer
        pairs = KV.findall(line)
        if pairs:
            found.update(dict(pairs))
    return found


def memory() -> Dict[str, int]:
    """MemAvailable / MemTotal / swap, from /proc/meminfo.

    Read from the kernel rather than from any container's view. On the PC the
    worker runs in Docker, which virtualises /proc, so the container sees the
    VM's total rather than the host's - that mistake once made a 5.2 GB model
    permanently unfittable and every job held forever while the gate looked
    perfectly healthy.
    """
    wanted = {
        "MemTotal": 0,
        "MemAvailable": 0,
        "SwapTotal": 0,
        "SwapFree": 0,
    }
    try:
        with open("/proc/meminfo", "r", encoding="utf-8") as handle:
            for line in handle:
                key, _, rest = line.partition(":")
                if key in wanted:
                    try:
                        wanted[key] = int(rest.split()[0]) * 1024
                    except (ValueError, IndexError):
                        pass
    except OSError:
        pass
    wanted["SwapUsed"] = wanted["SwapTotal"] - wanted["SwapFree"]
    return wanted


def reserve_bytes() -> int:
    raw = os.getenv("OPEN_NOTEBOOK_RESIDENCY_RESERVE_MB")
    if raw:
        try:
            return max(0, int(raw)) * 1024 * 1024
        except ValueError:
            pass
    return DEFAULT_RESERVE_MB * 1024 * 1024


def idle_seconds() -> int:
    raw = os.getenv("OPEN_NOTEBOOK_RESIDENCY_IDLE_SECONDS")
    if raw is None:
        return DEFAULT_IDLE_SECONDS
    try:
        return max(0, int(raw))
    except ValueError:
        return DEFAULT_IDLE_SECONDS


_CPU_CACHE: Optional[Dict[str, Any]] = None


def cpu_budget() -> Dict[str, Any]:
    """Ask the shell library for the shared CPU allowance.

    Read by running `onb-cpu` with a probe command, because that library is the
    single derivation the launchers use. Re-implementing the arithmetic here
    would be a second answer to "how many threads may inference use", and the
    two could disagree while each stayed self-consistent - which is the shape of
    every drift bug in this project.

    Cached: the topology does not move, and this is polled.
    """
    global _CPU_CACHE
    if _CPU_CACHE is not None:
        return _CPU_CACHE
    probe = (
        '. "$HOME/bin/onb-cpu" >/dev/null 2>&1 || true; '
        "if command -v cpu_summary >/dev/null 2>&1; then cpu_summary; fi"
    )
    code, out, _ = run(["sh", "-c", probe], 10.0)
    if code != 0 or not out.strip():
        _CPU_CACHE = {
            "available": False,
            "reason": "onb-cpu not installed or unreadable",
        }
        return _CPU_CACHE
    _CPU_CACHE = {
        "available": True,
        "summary": out.strip(),
        "cores": _int_from(out, "cores"),
        "big_cores": _int_from(out, "big"),
        "budget": _int_from(out, "budget"),
    }
    return _CPU_CACHE


_THREADS_CACHE: Dict[str, int] = {}


def role_threads(name: str) -> int:
    """Threads one class uses, from the derivation the launchers used.

    Read through the same shell library rather than stated here, so the number
    the gate charges against the budget is the number the server was started
    with. Cached because the topology does not move and this sits in a probe
    path.

    Falls back to 2 - what an 8-core device derives anyway - when the library is
    absent, so the budget stays coherent rather than one side guessing.
    """
    if not _THREADS_CACHE and cpu_budget().get("available"):
        probe = (
            '. "$HOME/bin/onb-cpu" >/dev/null 2>&1 || true; '
            "if command -v cpu_threads >/dev/null 2>&1; then "
            f"cpu_threads {name}; fi"
        )
        code, out, _ = run(["sh", "-c", probe], 10.0)
        if code == 0 and out.strip().isdigit():
            _THREADS_CACHE[name] = max(1, int(out.strip()))
    return _THREADS_CACHE.get(name, 2)


def _int_from(text: str, key: str) -> Optional[int]:
    """Pull an integer out of the library's one-line summary.

    Two orders, because the summary reads naturally either way: `cores 8` puts
    the value after the word, `(4 big: 4,5,6,7)` puts it before. Both are
    tried rather than guessing which, and neither matching returns None rather
    than a plausible wrong number.
    """
    import re

    after = re.search(rf"\b{key}\s+(\d+)", text)
    if after:
        return int(after.group(1))
    before = re.search(rf"\b(\d+)\s+{key}\b", text)
    return int(before.group(1)) if before else None


@dataclass
class Residency:
    """Residency state for one role."""

    role: str
    #: Weights are in RAM. False for a server that is answering with its model
    #: paged out to zram - a state that is neither "loaded" nor "down".
    resident: bool = False
    #: Something is answering on the role's port. True whenever `resident` is,
    #: and also true when the process is alive with its weights swapped out.
    serving: bool = False
    pid: str = "-"
    model: str = "-"
    disk_bytes: int = 0
    #: Resident set size in kB, straight from /proc. The measurement behind
    #: `resident`, published because it is the only number that cannot be
    #: argued with.
    rss_kb: int = 0
    #: Did *this supervisor* start it?
    #:
    #: The single most important field here, and the one whose absence caused
    #: the bug this fixes. `~/bin/onb` starts chat and embed deliberately at
    #: boot. This supervisor then found them idle for 300s - because no guest
    #: was calling `ensure_resident`, since residency was not enabled - and
    #: unloaded both. It was tearing down processes it had never started,
    #: five minutes after the operator started the stack on purpose.
    #:
    #: Two owners of one lifecycle is the actual defect. Collapsing two
    #: *implementations* of start/stop into one set of shell verbs was not
    #: enough, because ownership was still split. So the rule is narrow: this
    #: process only reaps what it loaded itself.
    owned: bool = False
    last_used: float = field(default_factory=time.monotonic)
    loaded_at: Optional[float] = None
    error: Optional[str] = None

    @property
    def resident_bytes(self) -> int:
        if not self.resident or not self.disk_bytes:
            return 0
        return int(self.disk_bytes * WEIGHT_OVERHEAD)


class Manager:
    """Decides, serialises and records what is loaded."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state: Dict[str, Residency] = {n: Residency(role=n) for n in ROLES}
        self._reaper: Optional[threading.Thread] = None
        self._stop = threading.Event()

    # -- observation ----------------------------------------------------

    def probe(self, role: Role) -> Residency:
        """Ask the role's own script what is true, and record the answer."""
        code, out, err = run(role.probe, PROBE_TIMEOUT)
        fields = parse_kv(out)
        state = self._state[role.name]
        # Absent or unparseable output is reported as not resident. The
        # alternative - carrying the last known answer forward - would let a
        # supervisor claim a model is loaded after the process died, which is
        # the failure this whole mechanism exists to prevent.
        state.resident = fields.get("resident") == "1" and code == 0
        state.pid = fields.get("pid", "-")
        state.model = fields.get("model", "-")
        # `serving` and `resident` are different questions and the supervisor
        # keeps both. A server whose weights are in zram answers /health and
        # /v1/models perfectly, so "serving" cannot stand in for "resident" -
        # conflating them is what made this report a 1.7 MB model as loaded.
        state.serving = fields.get("serving") == "1" or (state.resident and code == 0)
        try:
            state.rss_kb = int(fields.get("rss_kb", "0") or 0)
        except ValueError:
            state.rss_kb = 0
        if not state.resident:
            state.loaded_at = None
        elif code == 0 and state.loaded_at is None:
            state.loaded_at = time.time()
        return state

    def model_bytes(self, role: Role) -> int:
        code, out, _ = run(role.model_path, PROBE_TIMEOUT)
        if code != 0:
            return 0
        raw = parse_kv(out).get("bytes", "0")
        try:
            return int(raw)
        except ValueError:
            return 0

    def catalogue(self) -> Dict[str, Any]:
        """What is available and what is loaded, discovered at runtime.

        Sizes come from the filesystem, not a table. A hardcoded catalogue goes
        stale the moment a model is added or removed, and a stale size is worse
        than no size here - it is what the memory ceiling is computed from.
        """
        cpu = cpu_budget()
        roles: Dict[str, Any] = {}
        for name, role in ROLES.items():
            state = self.probe(role)
            disk = self.model_bytes(role)
            state.disk_bytes = disk
            roles[name] = {
                "role": name,
                "resident": state.resident,
                "serving": state.serving,
                # False means the launcher started it, and the idle reaper will
                # not touch it. Published because the alternative is a model
                # vanishing with nothing in any log to say who took it.
                "owned": state.owned,
                "pid": state.pid,
                "model": state.model,
                "disk_bytes": disk,
                "resident_bytes": state.resident_bytes,
                "rss_kb": state.rss_kb,
                "port": role.port,
                "priority": role.priority,
                "max_concurrent_jobs": role.max_concurrent_jobs,
                # What the launchers actually started this class with, from the
                # same derivation. Charged against the shared budget by the
                # guest's admission gate, so CPU and memory are held alike.
                "threads": role.threads,
                "idle_seconds": idle_seconds(),
                "last_used_age": round(time.monotonic() - state.last_used, 1),
            }
        return {"roles": roles, "cpu": cpu}

    def cpu(self) -> Dict[str, Any]:
        """The shared CPU allowance, for the guest's admission gate.

        Sourced from the same shell library the launchers use, so the gate holds
        a job against the same budget the launchers actually started with. A
        second derivation in Python would be a second answer.
        """
        return cpu_budget()

    def snapshot(self) -> Dict[str, Any]:
        return {
            "roles": self.catalogue()["roles"],
            "memory": memory(),
            "reserve_bytes": reserve_bytes(),
            "idle_seconds": idle_seconds(),
            "resident_total_bytes": sum(
                r["resident_bytes"] for r in self.catalogue()["roles"].values()
            ),
        }

    # -- decisions ------------------------------------------------------

    def fits(self, role: Role, disk_bytes: int) -> tuple[bool, str]:
        """Would loading this role leave enough behind? ADR-014 at load time."""
        if disk_bytes <= 0:
            return False, "model size unknown - refusing to guess"
        reading = memory()
        available = reading.get("MemAvailable", 0)
        if available <= 0:
            return False, "MemAvailable unreadable - refusing to load"
        needed = int(disk_bytes * WEIGHT_OVERHEAD)
        reserve = reserve_bytes()
        if available - needed < reserve:
            return False, (
                f"needs {needed // 1048576} MB, {available // 1048576} MB available, "
                f"reserve {reserve // 1048576} MB - held, not shrunk"
            )
        return True, ""

    def ensure(self, name: str) -> Dict[str, Any]:
        """Make `name` resident. Idempotent, and serialised across roles."""
        role = ROLES.get(name)
        if role is None:
            return {"ok": False, "role": name, "error": f"unknown role {name!r}"}

        with self._lock:
            state = self.probe(role)
            # Admission is about `serving`, not `resident`.
            #
            # A server whose weights are in zram will still answer a request -
            # it faults the pages back in, which costs time (6.9s measured for
            # the first generation) but works. Holding the job because the
            # model is not warm would turn a slow request into a stalled one,
            # and the caller cannot tell that apart from a real failure.
            #
            # So `serving` means proceed and `resident` means "cheap right
            # now"; they are reported separately because only the second is a
            # performance fact. Starting a second server here would also be
            # wrong - the weights are still mapped, just not resident.
            if state.serving:
                state.last_used = time.monotonic()
                return {
                    "ok": True,
                    "role": name,
                    "loaded": False,
                    "resident": state.resident,
                    "pid": state.pid,
                    "model": state.model,
                }

            disk = self.model_bytes(role)
            fits, why = self.fits(role, disk)
            if not fits:
                state.error = why
                return {"ok": False, "role": name, "loaded": False, "error": why}

            started = time.monotonic()
            code, out, err = run(role.load, LOAD_TIMEOUT)
            # Re-probe rather than trusting the exit code. The launchers have
            # been wrong about success before - `down` reported stopping a
            # server that was still holding its port - and the only trustworthy
            # answer to "is it up" is the same question asked again.
            state = self.probe(role)
            state.last_used = time.monotonic()
            if state.serving:
                state.error = None
                # Claim it. Only a role this supervisor started is ever a
                # candidate for the idle reaper, so the act of loading is what
                # creates the obligation to clean up.
                state.owned = True
                return {
                    "ok": True,
                    "role": name,
                    "loaded": True,
                    "resident": state.resident,
                    "pid": state.pid,
                    "model": state.model,
                    "elapsed_s": round(time.monotonic() - started, 2),
                }
            detail = (err or out or "").strip().splitlines()
            state.error = (
                f"load failed (exit {code}): {detail[-1] if detail else 'no output'}"
            )
            return {"ok": False, "role": name, "loaded": False, "error": state.error}

    def release(self, name: str) -> Dict[str, Any]:
        role = ROLES.get(name)
        if role is None:
            return {"ok": False, "role": name, "error": f"unknown role {name!r}"}
        with self._lock:
            state = self.probe(role)
            # Unload is keyed off `serving`, not `resident`: a swapped-out
            # server still holds its port and its mapped weights, and leaving
            # it up is exactly what an unload is meant to prevent.
            if not state.serving:
                return {"ok": True, "role": name, "unloaded": False}
            code, out, err = run(role.unload, LOAD_TIMEOUT)
            state = self.probe(role)
            # No longer ours to reap, whether or not the stop worked. Leaving
            # `owned` set after a failed unload would keep retrying forever
            # against a process it no longer controls.
            self._state[name].owned = False
            return {
                "ok": not state.serving,
                "role": name,
                "unloaded": not state.serving,
                "error": None
                if not state.serving
                else (
                    (err or out).strip() or f"still serving after unload (exit {code})"
                ),
            }

    def release_all(self) -> Dict[str, Any]:
        return {
            name: self.release(name)
            for name in sorted(ROLES, key=lambda n: -ROLES[n].priority)
        }

    # -- idle reaping ---------------------------------------------------

    def reap_idle(self) -> List[str]:
        """Unload what *we* loaded, once it has been idle past the threshold.

        `owned` is the whole filter. A role the launcher started is left alone
        however long it sits there: the operator asked for it, and this process
        did not put it there. Reaping it anyway is what made the stack's own
        boot-time servers disappear five minutes after a start, with the log
        saying `idle-unloaded chat, embed` and nothing else to explain it.

        The launcher remains the authority on what exists at boot. This is
        only ever a garbage collector for models this supervisor started on
        request.
        """
        limit = idle_seconds()
        if limit <= 0:
            return []
        reaped: List[str] = []
        for name in sorted(ROLES, key=lambda n: ROLES[n].priority):
            role = ROLES[name]
            state = self.probe(role)
            if not state.owned or not state.serving:
                continue
            if time.monotonic() - state.last_used < limit:
                continue
            outcome = self.release(name)
            if outcome.get("ok"):
                reaped.append(name)
        return reaped

    def _reap_loop(self) -> None:
        while not self._stop.wait(30.0):
            try:
                reaped = self.reap_idle()
                if reaped:
                    print(
                        f"[residency] idle-unloaded {', '.join(reaped)}",
                        flush=True,
                    )
            except Exception as exc:  # noqa: BLE001 - a reaper must not die
                print(f"[residency] reaper error: {exc}", file=sys.stderr, flush=True)

    def start_reaper(self) -> None:
        if self._reaper is not None:
            return
        if idle_seconds() <= 0:
            print("[residency] idle reaping disabled", flush=True)
            return
        self._reaper = threading.Thread(target=self._reap_loop, daemon=True)
        self._reaper.start()
        print(f"[residency] idle-unload after {idle_seconds()}s", flush=True)


MANAGER = Manager()


class Handler(BaseHTTPRequestHandler):
    """Loopback JSON. No auth: the only client is the guest API, and it shares
    this loopback, so binding to 127.0.0.1 is the access control."""

    server_version = "onb-residency/1"

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[residency] {fmt % args}", flush=True)

    def _send(self, code: int, payload: Dict[str, Any]) -> None:
        body = json.dumps(payload, indent=2).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's name
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        if path == "/":
            self._send(200, {"roles": sorted(ROLES), "state": MANAGER.snapshot()})
        elif path == "/state":
            self._send(200, MANAGER.snapshot())
        elif path == "/models":
            self._send(200, MANAGER.catalogue())
        elif path == "/health":
            self._send(200, {"ok": True, "roles": sorted(ROLES)})
        else:
            self._send(404, {"error": f"no such endpoint: {path}"})

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            self._send(400, {"ok": False, "error": "body is not JSON"})
            return
        if path != "/resident":
            self._send(404, {"error": f"no such endpoint: {path}"})
            return
        role = body.get("role")
        if not isinstance(role, str):
            self._send(400, {"ok": False, "error": "role must be a string"})
            return
        result = MANAGER.ensure(role)
        self._send(200 if result.get("ok") else 409, result)

    def do_DELETE(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        prefix = "/resident/"
        if not path.startswith(prefix):
            self._send(404, {"error": f"no such endpoint: {path}"})
            return
        role = path[len(prefix) :]
        result = MANAGER.release(role)
        self._send(200 if result.get("ok") else 409, result)


def main() -> int:
    port = PORT
    while port < PORT + 20:
        try:
            server = ThreadingHTTPServer((HOST, port), Handler)
            break
        except OSError as exc:
            print(
                f"[residency] :{port} unavailable ({exc}); trying {port + 1}",
                flush=True,
            )
            port += 1
    else:
        print("[residency] FATAL: no free port", file=sys.stderr)
        return 1

    MANAGER.start_reaper()
    print(
        f"[residency] listening on {HOST}:{port} "
        f"roles={','.join(sorted(ROLES))} "
        f"reserve={reserve_bytes() // 1048576}MB "
        f"idle={idle_seconds()}s",
        flush=True,
    )
    for role in sorted(ROLES, key=lambda n: ROLES[n].priority):
        state = MANAGER.probe(ROLES[role])
        state.disk_bytes = MANAGER.model_bytes(ROLES[role])
        print(
            f"[residency] {role}: {'resident' if state.resident else 'unloaded'} "
            f"pid={state.pid} model={state.model} "
            f"disk={state.disk_bytes // 1048576}MB "
            f"resident_est={state.resident_bytes // 1048576}MB",
            flush=True,
        )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        MANAGER._stop.set()
    return 0


if __name__ == "__main__":
    sys.exit(main())
