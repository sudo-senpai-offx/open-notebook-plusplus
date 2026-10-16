"""The dispatcher's lifecycle, mirroring `JobMonitor`.

Separate module from the dispatch decision because this one owns a thread and
a shutdown, and mixing the two made each harder to test than either is on its
own.

Deliberately *not* standalone. On the phone every process in
`termux-stack.sh` is `nohup ... &` with no supervisor and no restart policy,
so a separate dispatcher process that died stayed dead - a state the UI could
not distinguish from "busy". `JobMonitor` already runs inside the API process
on both devices, so following that pattern means the dispatcher inherits a
lifecycle already proven on hardware.

Failures are logged and retried rather than fatal. A pass that raises leaves
jobs `held`, which is the correct direction: nothing runs, and the reason is
visible in the status endpoint. The alternative - letting the loop die -
produces a device that is silently wedged, which is indistinguishable from a
legitimate memory hold.
"""

from __future__ import annotations

import asyncio
import os
import threading
from typing import Optional

from loguru import logger

from open_notebook.scheduler.dispatcher import (
    DispatchOutcome,
    dispatch_once,
    generation_model_bytes,
)

DEFAULT_INTERVAL = 5.0


class Dispatcher:
    """Runs `dispatch_once` on an interval until stopped."""

    def __init__(self, interval: Optional[float] = None) -> None:
        self._interval = (
            interval
            if interval is not None
            else float(os.getenv("OPEN_NOTEBOOK_DISPATCH_INTERVAL", DEFAULT_INTERVAL))
        )
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.last_outcome: Optional[DispatchOutcome] = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="onb-dispatcher", daemon=True
        )
        self._thread.start()
        logger.info(f"dispatcher started (interval {self._interval}s)")

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=self._interval + 2)
        logger.info("dispatcher stopped")

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def _run(self) -> None:
        # A fresh generation-model size per pass is deliberate but not free:
        # it is one short HTTP call to a local Ollama. Refreshed every 60s
        # rather than every pass, because the model does not change between
        # passes - but not cached forever either, since a device that reloads
        # a different model would otherwise be gated against the wrong size
        # indefinitely and that failure looks like an unrelated stall.
        last_lookup = 0.0
        generation_bytes: Optional[int] = None

        while not self._stop.is_set():
            import time

            now = time.monotonic()
            if now - last_lookup > 60:
                try:
                    generation_bytes = asyncio.run(generation_model_bytes())
                except Exception as exc:  # noqa: BLE001 - absence holds, never crashes
                    logger.debug(f"generation model size unavailable: {exc}")
                    generation_bytes = None
                last_lookup = now

            try:
                self.last_outcome = asyncio.run(
                    dispatch_once(generation_bytes=generation_bytes)
                )
            except Exception as exc:  # noqa: BLE001 - the loop must outlive a bad pass
                logger.warning(f"dispatcher pass failed: {exc}")

            self._stop.wait(self._interval)
