import asyncio
import contextvars
import os
import threading
from contextlib import suppress
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, Optional, Union

from loguru import logger

from open_notebook.database.repository import ensure_record_id, repo_query

current_command_id: ContextVar[Optional[str]] = ContextVar(
    "current_command_id", default=None
)


class JobCancelledError(Exception):
    """Raised when a job has been cancelled via cancel_requested flag."""

    pass


def get_current_command_id() -> Optional[str]:
    return current_command_id.get()


def set_current_command_id(
    command_id: Optional[str],
) -> "contextvars.Token[Optional[str]]":
    return current_command_id.set(command_id)


def reset_current_command_id(token: "contextvars.Token[Optional[str]]") -> None:
    current_command_id.reset(token)


async def report_progress(
    command_id: Optional[str],
    progress: Optional[int] = None,
    stage: Optional[str] = None,
) -> None:
    if not command_id:
        return

    now = datetime.now(timezone.utc).isoformat()
    patch: Dict[str, Any] = {"heartbeat_at": now}
    if progress is not None:
        # clamp 0-100
        try:
            p = int(progress)
        except (TypeError, ValueError):
            p = None
        if p is not None:
            if p < 0:
                p = 0
            if p > 100:
                p = 100
            patch["progress"] = p
    if stage is not None:
        patch["stage"] = str(stage)

    try:
        await repo_query(
            "UPDATE $id MERGE $patch",
            {
                "id": ensure_record_id(
                    f"command:{command_id}"
                    if not str(command_id).startswith("command:")
                    else command_id
                ),
                "patch": patch,
            },
        )
    except Exception as e:
        logger.debug(f"Failed to report progress for {command_id}: {e}")


# helper for contexts where command is given as record id string
async def report_progress_record(
    command_record_id: Union[str, Any],
    progress: Optional[int] = None,
    stage: Optional[str] = None,
) -> None:
    if not command_record_id:
        return
    cid = str(command_record_id)
    if ":" not in cid:
        cid = f"command:{cid}"
    await report_progress(cid, progress, stage)


async def raise_if_cancelled(command_id: Optional[str]) -> None:
    if not command_id:
        return
    try:
        rows = await repo_query(
            "SELECT cancel_requested FROM $id",
            {
                "id": ensure_record_id(
                    f"command:{command_id}"
                    if not str(command_id).startswith("command:")
                    else command_id
                )
            },
        )
        if rows and rows[0].get("cancel_requested") is True:
            raise JobCancelledError("Canceled by user request")
    except JobCancelledError:
        raise
    except Exception as e:
        logger.debug(f"Failed to check cancel for {command_id}: {e}")


def _get_heartbeat_interval() -> int:
    # B3: 120s default cuts heartbeat write-amplification ~4x vs 30s. The
    # 300s stale threshold keeps a 2.5-beat margin, and the monitor still
    # requires two consecutive stale sightings before marking failed.
    try:
        return int(os.getenv("OPEN_NOTEBOOK_HEARTBEAT_SECONDS", "120"))
    except Exception:
        return 120


class Heartbeat:
    def __init__(
        self,
        command_id: Optional[str],
        interval: Optional[int] = None,
        tick: Optional[Callable[[], Awaitable[None]]] = None,
    ) -> None:
        self._command_id = command_id
        self._interval = interval if interval is not None else _get_heartbeat_interval()
        self._tick = tick
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._started = False

    def start(self) -> None:
        if not self._command_id or self._started:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        self._started = True

    def stop(self) -> None:
        if not self._started:
            return
        self._stop.set()
        if self._thread:
            with suppress(Exception):
                self._thread.join(timeout=5)
        self._started = False

    def _run(self) -> None:
        # first tick
        self._call_tick()
        while not self._stop.wait(self._interval):
            self._call_tick()

    def _call_tick(self) -> None:
        async def _do() -> None:
            if self._tick is not None:
                await self._tick()
            else:
                await report_progress(self._command_id, progress=None, stage=None)

        try:
            asyncio.run(_do())
        except Exception as e:
            logger.debug(f"Heartbeat tick failed for {self._command_id}: {e}")


def get_exit_code_from_exception(exc: Exception) -> int:
    """Best effort to extract OOM/exit code from exception."""
    try:
        if isinstance(exc, BaseException) and hasattr(exc, "exit_code"):
            try:
                return int(getattr(exc, "exit_code"))
            except Exception:
                pass
    except Exception:
        pass
    msg = str(exc) or repr(exc)
    low = msg.lower()
    if "137" in msg or "oom" in low or "out of memory" in low or "killed" in low:
        return 137
    return 0


def is_oom_exception(exc: Exception) -> bool:
    return get_exit_code_from_exception(exc) == 137


#: Command statuses that hold the worker (shared by the API job monitor and
#: the idle-hibernate lease guard so both agree on what "active" means).
ACTIVE_STATUSES = ("new", "running")


def is_stale(row: Dict, threshold_seconds: int, now: datetime) -> bool:
    """Whether a command row's heartbeat is older than the stale threshold."""
    hb = row.get("heartbeat_at")
    if not hb:
        return True
    try:
        ts = datetime.fromisoformat(str(hb))
    except ValueError:
        return True
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return (now - ts).total_seconds() > threshold_seconds
