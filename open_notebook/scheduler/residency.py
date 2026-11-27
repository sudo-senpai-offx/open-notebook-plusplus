"""Ollama residency control for the Android 4 GB LMK safeguard (Week 5 Day 3).

On 4 GB Android devices, tesseract (~600 MB) and whisper-cli (~250-400 MB)
must never run concurrently with a loaded ~2.5 GB Ollama model, or the Low
Memory Killer will terminate the worker. This module decides whether Ollama is
resident in RAM and can unload it (`keep_alive: 0`) before heavy/deferred jobs
run, then warm-reload it once the queue drains so chat stays snappy.

Two layers:

* :class:`OllamaResidency` — a :class:`typing.Protocol` declaring the
  residency surface (is_loaded, loaded_models, unload, reload, health).
* :class:`HttpOllamaResidency` — the real implementation, talking to Ollama's
  loopback HTTP API (`/api/ps`, `/api/generate`).
* :class:`MockResidency` — a deterministic in-memory double for Tier-1 CI
  tests (no live Ollama binary required).

Failure is always a hold: if Ollama cannot be reached or unloaded, heavy
jobs stay held. Nothing is ever silently executed against an absent model.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any, List, Optional, Protocol, runtime_checkable

import httpx
from loguru import logger

DEFAULT_OLLAMA_HOST = "http://localhost:11434"
UNLOAD_POLL_INTERVAL_SECONDS = 0.5
UNLOAD_TIMEOUT_SECONDS = 30.0
RELOAD_TIMEOUT_SECONDS = 30.0


@runtime_checkable
class OllamaResidency(Protocol):
    """Surface for deciding whether Ollama is resident in RAM.

    Implementations decide whether a ~2.5 GB Ollama model is loaded and can
    unload/reload it so heavy lanes (OCR, STT) can run without the Low Memory
    Killer terminating the worker on 4 GB Android devices.
    """

    async def is_loaded(self) -> bool:
        """Whether any model is currently resident in Ollama's RAM."""

    async def loaded_models(self) -> List[str]:
        """Names of the models currently resident (may be empty)."""

    async def unload(self) -> bool:
        """Unload all resident models via ``keep_alive: 0``.

        Returns True when Ollama reports no loaded models after unloading,
        False if the unload failed or timed out. Callers treat False as a
        hold signal.
        """

    async def reload(self, model: Optional[str] = None) -> bool:
        """Warm-reload `model` (or the last unloaded model) with
        ``keep_alive: "5m"`` so the next chat does not cold-start.

        Returns True on success, False on failure. Failure is non-fatal:
        chat will warm Ollama on its next request.
        """

    async def health(self) -> bool:
        """Whether Ollama is reachable at all (liveness probe)."""


class HttpOllamaResidency:
    """HttpOllamaResidency talks to Ollama's loopback HTTP API.

    * ``is_loaded``/``loaded_models``: ``GET /api/ps`` (5 s timeout).
    * ``unload``: ``POST /api/generate`` with ``{"keep_alive": 0}`` per model,
      then poll ``/api/ps`` every 0.5 s (up to 30 s) until empty.
    * ``reload``: ``POST /api/generate`` with ``{"prompt": "", "keep_alive": "5m"}``
      (30 s timeout) to warm the model without doing inference.
    """

    def __init__(
        self,
        base_url: Optional[str] = None,
        timeout: float = 5.0,
    ) -> None:
        self.base_url = (
            (base_url or os.getenv("OLLAMA_HOST", DEFAULT_OLLAMA_HOST)).rstrip("/")
        )
        self.timeout = timeout
        #: Name of the last model this instance unloaded, so reload() knows
        #: what to restore. None when nothing has been unloaded yet.
        self.last_unloaded_model: Optional[str] = None

    async def is_loaded(self) -> bool:
        return bool(await self.loaded_models())

    async def loaded_models(self) -> List[str]:
        """Names of models resident in Ollama's RAM, or [] on any failure."""
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.get(f"{self.base_url}/api/ps")
        except Exception:  # noqa: BLE001 - unreachable Ollama = not loaded
            logger.debug(f"Ollama /api/ps unreachable at {self.base_url}")
            return []
        if response.status_code != 200:
            logger.debug(f"Ollama /api/ps returned {response.status_code}")
            return []
        try:
            payload: dict[str, Any] = response.json()
        except ValueError:
            logger.debug("Ollama /api/ps returned non-JSON")
            return []
        models = payload.get("models")
        if not isinstance(models, list):
            return []
        names: List[str] = []
        for entry in models:
            if not isinstance(entry, dict):
                continue
            name = entry.get("name")
            if isinstance(name, str) and name:
                names.append(name)
        return names

    async def unload(self) -> bool:
        """Unload every resident model; return True only when none remain."""
        models = await self.loaded_models()
        if not models:
            self.last_unloaded_model = None
            return True

        success = True
        for name in models:
            try:
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    await client.post(
                        f"{self.base_url}/api/generate",
                        json={"model": name, "keep_alive": 0},
                    )
                self.last_unloaded_model = name
            except Exception as exc:  # noqa: BLE001 - one model fails, still try others
                logger.warning(f"Ollama unload of {name} failed: {exc}")
                success = False

        # Poll until /api/ps reports no models (up to UNLOAD_TIMEOUT_SECONDS).
        polls = int(UNLOAD_TIMEOUT_SECONDS / UNLOAD_POLL_INTERVAL_SECONDS)
        for _ in range(polls):
            await asyncio.sleep(UNLOAD_POLL_INTERVAL_SECONDS)
            if not await self.is_loaded():
                return success
        logger.warning(
            f"Ollama still reports loaded models after {UNLOAD_TIMEOUT_SECONDS}s"
        )
        return False

    async def reload(self, model: Optional[str] = None) -> bool:
        """Warm-reload `model` (or the last unloaded model) with keep_alive 5m.

        Non-failure is logged; callers treat False as "chat will warm Ollama
        on its next request."
        """
        target = model or self.last_unloaded_model
        if not target:
            logger.debug("Ollama reload requested but no model to restore")
            return True
        try:
            async with httpx.AsyncClient(timeout=RELOAD_TIMEOUT_SECONDS) as client:
                await client.post(
                    f"{self.base_url}/api/generate",
                    json={"model": target, "prompt": "", "keep_alive": "5m"},
                )
        except Exception as exc:  # noqa: BLE001 - reload failure is non-fatal
            logger.warning(f"Ollama reload of {target} failed: {exc}")
            return False
        logger.info(f"Ollama model {target} re-warmed with keep_alive 5m")
        return True

    async def health(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.get(f"{self.base_url}/api/tags")
            return response.status_code == 200
        except Exception:  # noqa: BLE001 - unreachable = unhealthy
            return False


class MockResidency:
    """Deterministic in-memory residency double for Tier-1 CI tests.

    Tracks ``loaded``, ``unload_calls``, ``reload_calls``, and supports
    optional failure injection so the dispatcher's hold/admit/reload
    lifecycle can be exercised without a live Ollama binary.
    """

    def __init__(
        self,
        loaded: bool = False,
        fail_unload: bool = False,
        fail_reload: bool = False,
    ) -> None:
        self._loaded = loaded
        self.unload_calls = 0
        self.reload_calls = 0
        self.fail_unload = fail_unload
        self.fail_reload = fail_reload
        self.last_unloaded_model: Optional[str] = None
        self.last_reload_model: Optional[str] = None

    async def is_loaded(self) -> bool:
        return self._loaded

    async def loaded_models(self) -> List[str]:
        return ["llama3"] if self._loaded else []

    async def unload(self) -> bool:
        self.unload_calls += 1
        if self.fail_unload:
            return False
        self._loaded = False
        self.last_unloaded_model = "llama3"
        return True

    async def reload(self, model: Optional[str] = None) -> bool:
        self.reload_calls += 1
        self.last_reload_model = model
        if self.fail_reload:
            return False
        self._loaded = True
        return True

    async def health(self) -> bool:
        return self._loaded
