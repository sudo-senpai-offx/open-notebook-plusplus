"""Guest-side client for the Termux residency supervisor.

Why the guest has to ask
------------------------
The model servers live in Termux, outside proot, and proot cannot signal a
Termux process (`Operation not permitted`) or read its `/proc`. The API process
therefore *cannot* start a model, only decide whether one should be started and
ask for it. That is the whole reason this module exists, and it is why the
transport is loopback rather than anything cleverer: proot does not create a
network namespace, so the guest's 127.0.0.1 is the same loopback Termux sees.

Failure is a hold, never a shrug
-------------------------------
If the supervisor is unreachable, times out, or refuses a load, the caller gets
`ok=False` and holds the job. It must not fall back to "assume it is loaded" -
that is precisely the state the supervisor was added to prevent, and the
symptom of getting it wrong is a job that runs against nothing and reports
success.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from loguru import logger

DEFAULT_URL = "http://127.0.0.1:8099"
DEFAULT_TIMEOUT = 60.0
#: Probing a role is one shell call; loading is a model read off disk. The
#: supervisor's own load timeout is 45 s, so the client must outlast it or a
#: slow-but-successful load would be reported as a failure and the job held.
# The client's budget must outlast the supervisor's worst case.
#
# The supervisor's path to a resident role is: check the model (8s), release the
# wrong one (15s, waiting on a SIGTERM that whisper.cpp may not honour), size the
# new model (8s), load it (45s), probe again (8s) - and the launcher's own
# health wait allows 60s for a 547 MB model to load, which happens *outside* the
# supervisor's load budget. So the supervisor can take well over ninety seconds
# to answer, and a client that gives up at ninety sees a timeout on a load that
# was about to succeed.
#
# Four minutes is generous but bounded, and it is the difference between "the
# model was selected" and "the selection timed out and the old model was used
# instead" - which is indistinguishable from the feature not working.
DEFAULT_LOAD_TIMEOUT = 240.0


def supervisor_url() -> str:
    """Where the residency supervisor listens, if it is configured.

    The supervisor runs on the *phone* and the guest app reaches it over the
    shared loopback - proot does not isolate the network namespace, so
    127.0.0.1 inside the guest is the phone's. That is the same path the app
    already uses to reach the speech servers.
    """
    return os.getenv("OPEN_NOTEBOOK_RESIDENCY_URL", DEFAULT_URL) or DEFAULT_URL


def model_selection_enabled() -> bool:
    """Should the app ask the supervisor for a *particular* model?

    Separate from `enabled()`, which is the full residency regime: the
    admission gate, the reaper, and the app asking for every role.

    This one is narrower and is what the per-language speech tier needs. The
    app has to be able to say "I need the Mandarin model" and have the phone
    load it; nothing else about residency has to change. Keeping them apart
    means the tier can be switched on without the reaper starting to unload
    the chat and embedding servers between uses, which would be a real
    behaviour change on a device where those currently survive until reboot.
    """
    raw = os.getenv("OPEN_NOTEBOOK_RESIDENCY_MODEL_SELECTION", "false")
    return raw.strip().lower() in ("1", "true", "yes", "on")


def enabled() -> bool:
    """Is residency control switched on for this device?

    Off by default, matching how every other new capability in this codebase is
    gated (`USE_ROUTER`, `HIBERNATE_ENABLE`, the training flags). A deployment
    that has no supervisor must behave exactly as it did before, and a flag
    defaulting to on would make "no supervisor running" a silent behaviour
    change on the PC.
    """
    raw = os.getenv("OPEN_NOTEBOOK_RESIDENCY_ENABLED", "false")
    return raw.strip().lower() in ("1", "true", "yes", "on")


def base_url() -> str:
    return (
        os.getenv("OPEN_NOTEBOOK_RESIDENCY_URL", DEFAULT_URL) or DEFAULT_URL
    ).rstrip("/")


def timeout() -> float:
    raw = os.getenv("OPEN_NOTEBOOK_RESIDENCY_TIMEOUT")
    if raw:
        try:
            value = float(raw)
            if value > 0:
                return value
        except ValueError:
            pass
    return DEFAULT_LOAD_TIMEOUT


#: Which residency role serves a given footprint key.
#:
#: These are the keys `scheduler.footprint.job_footprint` produces: a category
#: key when the job's model is resolved by the caller, or one of the two
#: sentinels. Mapping here rather than in the dispatcher keeps the knowledge of
#: "which server does this work need" in one place next to the client's other
#: role-shaped concerns.
#:
#: `__no_model__` and `__unknown__` deliberately map to nothing. The first is
#: plumbing and needs no model; the second is an unresolved job whose class is
#: not knowable, and guessing one would make an uncertain job look schedulable.
#:
#: `transcription` is deliberately absent even though a media source does need
#: the STT model. Whether a `process_source` job needs speech-to-text is a fact
#: about the file, and the file is not opened until the job runs — so charging
#: every text source 256 MB to cover the minority that turns out to be audio
#: would hold work that has nothing to do with speech. The transcription client
#: asks for residency at the point of use instead, which is the same place the
#: answer becomes knowable.
MODEL_ROLE: Dict[str, str] = {
    "generation": "chat",
    "embedding": "embed",
    "synthesis": "tts",
}


def role_for_model(key: str) -> Optional[str]:
    return MODEL_ROLE.get(key)


def roles_for_models(keys: Any) -> List[str]:
    """Every role a set of footprint keys needs, de-duplicated."""
    found: List[str] = []
    for key in keys:
        role = role_for_model(str(key))
        if role and role not in found:
            found.append(role)
    return found


@dataclass(frozen=True)
class ResidencyResult:
    ok: bool
    role: str
    loaded: bool = False
    #: Weights in RAM. Distinct from `ok`: a role can be serving perfectly
    #: while paged out to zram, which is slower but not a failure. Conflating
    #: the two is what made the first version of this report a 1.7 MB model as
    #: loaded.
    resident: bool = False
    pid: str = "-"
    model: str = "-"
    error: Optional[str] = None

    def hold_reason(self) -> str:
        """A short, stable string for `held_reason`.

        Kept short and prefix-tagged because this value is shown verbatim in the
        status panel, and a full shell error there is unreadable next to every
        other reason.
        """
        if self.ok:
            return ""
        detail = (self.error or "unknown failure").splitlines()[0][:120]
        return f"residency:{self.role}:{detail}"


async def select_model(role: str, variant: str) -> ResidencyResult:
    """Ask the supervisor to load `role` with a particular model.

    Unlike `ensure_resident`, this talks to the supervisor even when the full
    residency regime is off. The difference matters: `ensure_resident` answers
    "is this role resident" and short-circuits to `ok=True` when residency is
    disabled, because a device with no supervisor must behave exactly as it did
    before. But *selecting a model* is not a residency-regime decision - it is
    one specific request, and the supervisor is the only thing that can answer
    it, because the model files are on the phone.

    The supervisor reloads the role when the running model is not the one the
    variant calls for, so this is also how a tier change happens.
    """
    if not variant:
        return ResidencyResult(ok=True, role=role)
    try:
        import httpx

        async with httpx.AsyncClient(timeout=timeout()) as client:
            response = await client.post(
                f"{supervisor_url()}/resident",
                json={"role": role, "variant": variant},
            )
    except Exception as exc:  # noqa: BLE001 - unreachable supervisor is a hold
        # An f-string, not %-style args. This project uses loguru, which does
        # not format %-style arguments: the placeholders came out literally in
        # the log, so the one line that says *why* the supervisor was
        # unreachable said "%s" instead of the reason.
        # The type is included because httpx's ReadTimeout has an empty
        # str(): the message ended with a bare colon and named no cause.
        logger.debug(
            f"residency supervisor unreachable for {role}: {type(exc).__name__}: {exc}"
        )
        return ResidencyResult(
            ok=False,
            role=role,
            error=f"supervisor unreachable: {type(exc).__name__}",
        )

    if response.status_code == 409:
        try:
            payload: Dict[str, Any] = response.json()
        except ValueError:
            payload = {}
        return ResidencyResult(
            ok=False,
            role=role,
            error=str(payload.get("error") or "supervisor refused the load"),
        )
    if response.status_code != 200:
        return ResidencyResult(
            ok=False, role=role, error=f"supervisor returned {response.status_code}"
        )
    try:
        payload = response.json()
    except ValueError:
        return ResidencyResult(ok=False, role=role, error="supervisor sent non-JSON")
    return ResidencyResult(
        ok=bool(payload.get("ok")),
        role=str(payload.get("role") or role),
        loaded=bool(payload.get("loaded")),
        resident=bool(payload.get("resident")),
        pid=str(payload.get("pid") or "-"),
        model=str(payload.get("model") or "-"),
        error=None if payload.get("ok") else str(payload.get("error") or "load failed"),
    )


async def touch(role: str, variant: str = "") -> None:
    """Tell the supervisor a role is still in use.

    The reaper unloads a role the supervisor loaded once it has been idle for
    five minutes, and it measures idle from the last `ensure`. But a
    transcription is one long request: a ten-minute lecture through the large
    model runs for well over an hour, and the supervisor cannot see a request
    in flight because the guest talks to the speech server directly.

    So without this, the reaper kills the model *while it is being used*, and
    the request fails with a connection error - the same symptom as the model
    never being loaded, and just as hard to read.

    A touch is the same request as `ensure`, which is idempotent when the role
    is already serving the right model.
    """
    try:
        await select_model(role, variant)
    except Exception:  # noqa: BLE001 - a failed touch must not fail the job
        logger.debug(f"could not touch the {role} role")


async def ensure_resident(role: str, variant: str = "") -> ResidencyResult:
    """Ask the supervisor to make `role` loaded.

    `variant` selects which model the role loads, when it has more than one.
    For STT it is the language of the job, and the tier follows from it:
    measured on the device, Mandarin at the small tier scores CER 0.222 - fluent
    Mandarin that is not what was said - against CER 0.000 at the large tier,
    for five times the time. The supervisor reloads the role when the running
    model is not the one the variant calls for.

    Never raises. Every failure mode - supervisor down, load refused for
    memory, load timed out - is an `ok=False` the caller holds on.
    """
    if not enabled():
        return ResidencyResult(ok=True, role=role)

    try:
        import httpx

        async with httpx.AsyncClient(timeout=timeout()) as client:
            response = await client.post(
                f"{base_url()}/resident",
                json={"role": role, "variant": variant},
            )
    except Exception as exc:  # noqa: BLE001 - unreachable supervisor is a hold
        logger.debug(f"residency supervisor unreachable for {role}: {exc}")
        return ResidencyResult(
            ok=False,
            role=role,
            error=f"supervisor unreachable: {type(exc).__name__}",
        )

    if response.status_code == 409:
        try:
            payload: Dict[str, Any] = response.json()
        except ValueError:
            payload = {}
        return ResidencyResult(
            ok=False,
            role=role,
            error=str(payload.get("error") or "supervisor refused the load"),
        )
    if response.status_code != 200:
        return ResidencyResult(
            ok=False, role=role, error=f"supervisor returned {response.status_code}"
        )
    try:
        payload = response.json()
    except ValueError:
        return ResidencyResult(ok=False, role=role, error="supervisor sent non-JSON")
    return ResidencyResult(
        ok=bool(payload.get("ok")),
        role=str(payload.get("role") or role),
        loaded=bool(payload.get("loaded")),
        resident=bool(payload.get("resident")),
        pid=str(payload.get("pid") or "-"),
        model=str(payload.get("model") or "-"),
        error=None if payload.get("ok") else str(payload.get("error") or "load failed"),
    )


@dataclass(frozen=True)
class RolePolicy:
    """Per-class admission limits, owned by the supervisor.

    Fetched rather than duplicated. The same numbers appear in
    `termux-supervisor.py` and are derived there from the device's actual core
    topology, and the launchers start the servers with those same figures. A
    copy in the guest would drift the moment one side changed - and the drift
    would be silent, because each side would still be self-consistent.
    """

    role: str
    priority: int = 50
    max_concurrent_jobs: int = 1
    #: Threads this class holds while a job of it runs. Charged against the
    #: shared CPU budget alongside memory, so the two resources are held the
    #: same way and for the same reason.
    threads: int = 2


async def policies() -> Optional[Dict[str, RolePolicy]]:
    """Current per-class policy, or None when it cannot be had.

    None means "do not enforce per-class limits", which is the right response
    to an unreachable supervisor: enforcing limits from a stale copy would hold
    work for reasons that no longer exist.
    """
    if not enabled():
        return None
    try:
        import httpx

        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(f"{base_url()}/models")
    except Exception as exc:  # noqa: BLE001 - no policy is no per-class limit
        logger.debug(f"residency catalogue unavailable: {exc}")
        return None
    if response.status_code != 200:
        return None
    try:
        payload = response.json()
    except ValueError:
        return None
    roles = payload.get("roles")
    if not isinstance(roles, dict):
        return None
    found: Dict[str, RolePolicy] = {}
    for name, entry in roles.items():
        if not isinstance(entry, dict):
            continue
        try:
            found[str(name)] = RolePolicy(
                role=str(name),
                priority=int(entry.get("priority", 50)),
                max_concurrent_jobs=int(entry.get("max_concurrent_jobs", 1)),
                threads=max(0, int(entry.get("threads", 2))),
            )
        except (TypeError, ValueError):
            continue
    return found or None


async def cpu_budget() -> Optional[int]:
    """The shared thread allowance, or None when it cannot be had.

    None means no CPU limit is enforced. That is the safe direction: the gate
    would otherwise hold work against a budget derived from a core count
    guessed somewhere other than the device, and the hold would have no cause
    the operator could find.
    """
    if not enabled():
        return None
    try:
        import httpx

        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(f"{base_url()}/models")
    except Exception as exc:  # noqa: BLE001 - no budget is no cpu limit
        logger.debug(f"residency cpu budget unavailable: {exc}")
        return None
    if response.status_code != 200:
        return None
    try:
        payload = response.json()
    except ValueError:
        return None
    cpu = payload.get("cpu")
    if not isinstance(cpu, dict) or not cpu.get("available"):
        return None
    budget_raw = cpu.get("budget")
    if not isinstance(budget_raw, (int, str)):
        return None
    try:
        value = int(budget_raw)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


async def state() -> Optional[Dict[str, Any]]:
    """Residency + memory, for the status endpoint. None when unavailable."""
    if not enabled():
        return None
    try:
        import httpx

        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(f"{base_url()}/state")
    except Exception as exc:  # noqa: BLE001 - the panel shows absence honestly
        logger.debug(f"residency state unavailable: {exc}")
        return None
    if response.status_code != 200:
        return None
    try:
        payload = response.json()
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None
