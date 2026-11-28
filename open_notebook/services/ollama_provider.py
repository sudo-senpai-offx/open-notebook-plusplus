"""
Ollama local LLM provider implementation.

Runs models locally via the Ollama server (http://localhost:11434).
Supports chat completions, model listing, and concurrency control
for resource-constrained devices (3GB min system RAM, 8GB min ON++ storage).

On-demand model management:
- pull_model() — download a model on-demand
- unload_model() / unload_all() — free RAM by unloading models
- set_keep_alive() — control model residency (0 = unload immediately)
- is_model_loaded() / ensure_model_loaded() — check/ensure residency
- rapid loading/unloading without compromise — local processing only on-demand

Concurrency limits:
- Max 1 concurrent Ollama request (to avoid starving resources on low-RAM devices)
- Request queue with timeout to prevent indefinite blocking
- Memory-aware: respects device RAM constraints
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any, Dict, List, Optional

import httpx
from loguru import logger

from open_notebook.ai.models import ProcessingFlags
from open_notebook.ai.providers import (
    BaseProvider,
    ProviderConfig,
    ProviderSendResult,
    PROVIDER_OLLAMA,
)

#: Default Ollama server URL
DEFAULT_OLLAMA_URL = "http://localhost:11434"

#: Concurrency limits for resource-constrained devices
_MAX_CONCURRENT = 1  # One request at a time on low-RAM devices
_REQUEST_TIMEOUT = 300.0  # 5 minutes for model loading/inference


class OllamaProvider(BaseProvider):
    """Ollama local LLM provider.

    Runs models locally via the Ollama HTTP API. No API key needed.
    Designed for resource-constrained devices with concurrency control
    to avoid exhausting limited RAM.

    On-demand model lifecycle:
    - Models are loaded only when needed (keep_alive=0 by default)
    - After each request, model is unloaded to free RAM
    - pull_model() downloads models on demand
    - unload_model() explicitly frees a model from memory
    """

    def __init__(self, config: ProviderConfig):
        super().__init__(PROVIDER_OLLAMA, config)
        self._semaphore: Optional[asyncio.Semaphore] = None
        self._semaphore_lock = asyncio.Lock()
        self._base_url: Optional[str] = None
        self._default_model: Optional[str] = None
        # Track which models we've pulled (to avoid re-pulling)
        self._pulled_models: set[str] = set()

    async def _get_semaphore(self) -> asyncio.Semaphore:
        """Get or create the concurrency semaphore."""
        if self._semaphore is None:
            async with self._semaphore_lock:
                if self._semaphore is None:
                    self._semaphore = asyncio.Semaphore(_MAX_CONCURRENT)
        return self._semaphore

    async def _get_base_url(self) -> str:
        """Resolve the Ollama base URL from flags or environment."""
        if self._base_url is not None:
            return self._base_url
        flags = await ProcessingFlags.get_instance()
        ollama_url = getattr(flags, "ollama_url", None) or os.getenv(
            "OLLAMA_BASE_URL", DEFAULT_OLLAMA_URL
        )
        self._base_url = ollama_url.rstrip("/")
        return self._base_url

    async def _get_default_model(self) -> str:
        """Resolve the default Ollama model from flags or config."""
        if self._default_model is not None:
            return self._default_model
        flags = await ProcessingFlags.get_instance()
        self._default_model = (
            getattr(flags, "ollama_default_model", None)
            or self.config.default_model
            or "llama3.2:3b"
        )
        return self._default_model

    # ------------------------------------------------------------------
    # On-demand model lifecycle management
    # ------------------------------------------------------------------

    async def is_model_loaded(self, model_name: Optional[str] = None) -> bool:
        """Check if a model is currently loaded in Ollama memory."""
        model = model_name or await self._get_default_model()
        try:
            base_url = await self._get_base_url()
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(f"{base_url}/api/ps")
                if resp.status_code == 200:
                    data = resp.json()
                    models = data.get("models", [])
                    return any(m.get("name", "") == model for m in models)
        except Exception:  # noqa: BLE001
            pass
        return False

    async def set_keep_alive(
        self, model_name: Optional[str] = None, duration: str = "0s"
    ) -> bool:
        """Set how long a model stays loaded in memory.

        Args:
            model_name: Model to configure (default: default model)
            duration: Keep-alive duration (e.g. "0s" = unload immediately,
                      "5m" = 5 minutes, "-1" = never unload)

        Returns True if the request succeeded.
        """
        model = model_name or await self._get_default_model()
        try:
            base_url = await self._get_base_url()
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(
                    f"{base_url}/api/generate",
                    json={"model": model, "keep_alive": duration},
                )
                return resp.status_code == 200
        except Exception:  # noqa: BLE001
            pass
        return False

    async def unload_model(self, model_name: Optional[str] = None) -> bool:
        """Unload a model from Ollama memory to free RAM.

        Sets keep_alive to "0s" which causes Ollama to unload the model
        immediately after the current request completes.
        """
        return await self.set_keep_alive(model_name, "0s")

    async def unload_all_models(self) -> bool:
        """Unload all models from Ollama memory."""
        try:
            base_url = await self._get_base_url()
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(f"{base_url}/api/ps")
                if resp.status_code != 200:
                    return False
                data = resp.json()
                models = data.get("models", [])
                success = True
                for m in models:
                    name = m.get("name", "")
                    if name:
                        if not await self.unload_model(name):
                            success = False
                return success
        except Exception:  # noqa: BLE001
            pass
        return False

    async def pull_model(
        self,
        model_name: str,
        timeout: float = 600.0,
    ) -> Dict[str, Any]:
        """Pull (download) a model on-demand.

        Args:
            model_name: Model name to pull (e.g. "llama3.2:1b", "nomic-embed-text")
            timeout: Maximum time to wait for the pull to complete

        Returns dict with status info.
        """
        if model_name in self._pulled_models:
            return {"status": "already_pulled", "model": model_name}

        try:
            base_url = await self._get_base_url()
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(
                    f"{base_url}/api/pull",
                    json={"model": model_name},
                )
                if resp.status_code == 200:
                    self._pulled_models.add(model_name)
                    return {"status": "pulled", "model": model_name}
                return {
                    "status": "error",
                    "model": model_name,
                    "error": f"HTTP {resp.status_code}: {resp.text[:200]}",
                }
        except asyncio.TimeoutError:
            return {
                "status": "timeout",
                "model": model_name,
                "error": f"Pull timed out after {timeout}s",
            }
        except Exception as exc:  # noqa: BLE001
            logger.error(f"Ollama pull failed for {model_name}: {exc}")
            return {
                "status": "error",
                "model": model_name,
                "error": str(exc),
            }

    async def ensure_model_loaded(
        self,
        model_name: Optional[str] = None,
        auto_pull: bool = True,
    ) -> Dict[str, Any]:
        """Ensure a model is loaded in Ollama memory.

        If the model is not available locally and auto_pull is True,
        downloads it first. Then ensures it's loaded (keeps alive).

        Returns dict with status info.
        """
        model = model_name or await self._get_default_model()

        # Check if already loaded
        if await self.is_model_loaded(model):
            return {"status": "loaded", "model": model}

        # Check if model exists locally
        local_models = await self.list_models()
        local_names = {m.get("name", "") for m in local_models}

        if model not in local_names:
            if auto_pull:
                pull_result = await self.pull_model(model)
                if pull_result["status"] != "pulled":
                    return pull_result
            else:
                return {
                    "status": "not_found",
                    "model": model,
                    "error": f"Model {model} not found locally and auto_pull disabled",
                }

        # Model exists locally, load it (a generate request with keep_alive loads it)
        load_result = await self.set_keep_alive(model, "5m")
        if load_result:
            return {"status": "loaded", "model": model}
        return {
            "status": "error",
            "model": model,
            "error": "Failed to load model",
        }

    # ------------------------------------------------------------------
    # Chat
    # ------------------------------------------------------------------

    async def is_available(self) -> bool:
        """Ollama is available if the server is reachable and has at least one model."""
        try:
            base_url = await self._get_base_url()
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(f"{base_url}/api/tags")
                if resp.status_code == 200:
                    data = resp.json()
                    models = data.get("models", [])
                    return len(models) > 0
        except Exception:  # noqa: BLE001
            pass
        return False

    async def _send_message_impl(
        self,
        message: str,
        *,
        session_id: Optional[str] = None,
        thinking_enabled: bool = False,
        ref_file_ids: Optional[List[str]] = None,
        timeout: float = 120.0,
        model: Optional[str] = None,
        pc_fallback: bool = False,
    ) -> ProviderSendResult:
        """Send a message to the local Ollama model.

        Uses the /api/chat endpoint for non-streaming chat.
        Concurrency is limited to 1 request at a time to protect
        resource-constrained devices.

        On-demand: ensures the model is loaded before sending, then
        unloads it after the response to free RAM (keep_alive=0s).
        """
        model_name = model or await self._get_default_model()
        base_url = await self._get_base_url()

        # Ensure model is loaded before sending
        load_result = await self.ensure_model_loaded(model_name, auto_pull=True)
        if load_result["status"] == "error":
            return ProviderSendResult(
                ok=False,
                error=f"Ollama model not available: {load_result.get('error')}",
                used_provider=self.provider_id,
            )

        sem = await self._get_semaphore()
        async with sem:
            try:
                payload: Dict[str, Any] = {
                    "model": model_name,
                    "messages": [{"role": "user", "content": message}],
                    "stream": False,
                    "options": {
                        "temperature": 0.7,
                        "num_predict": 2048,
                    },
                }
                if thinking_enabled:
                    payload["options"]["think"] = True

                async with httpx.AsyncClient(timeout=timeout) as client:
                    resp = await client.post(
                        f"{base_url}/api/chat",
                        json=payload,
                    )

                # Unload model after response to free RAM (on-demand)
                await self.unload_model(model_name)

                if resp.status_code == 429:
                    return ProviderSendResult(
                        ok=False,
                        error="Ollama server rate limited. Wait and retry.",
                        rate_limited=True,
                        used_provider=self.provider_id,
                    )
                if resp.status_code != 200:
                    return ProviderSendResult(
                        ok=False,
                        error=f"Ollama returned HTTP {resp.status_code}: {resp.text[:200]}",
                        used_provider=self.provider_id,
                    )

                data = resp.json()
                message_data = data.get("message", {})
                response_text = message_data.get("content", "")

                if not response_text:
                    return ProviderSendResult(
                        ok=False,
                        error="Empty response from Ollama.",
                        used_provider=self.provider_id,
                    )

                return ProviderSendResult(
                    ok=True,
                    response=response_text,
                    thinking=message_data.get("thinking"),
                    session_id=session_id,
                    used_provider=self.provider_id,
                    ref_file_ids=ref_file_ids or [],
                )

            except asyncio.TimeoutError:
                await self.unload_model(model_name)
                return ProviderSendResult(
                    ok=False,
                    error=f"Ollama request timed out after {timeout}s.",
                    used_provider=self.provider_id,
                )
            except httpx.ConnectError:
                await self.unload_model(model_name)
                return ProviderSendResult(
                    ok=False,
                    error=(
                        "Cannot connect to Ollama server. "
                        "Ensure Ollama is running (ollama serve) and reachable."
                    ),
                    used_provider=self.provider_id,
                )
            except Exception as exc:  # noqa: BLE001
                await self.unload_model(model_name)
                logger.error(f"Ollama send failed: {exc}")
                return ProviderSendResult(
                    ok=False,
                    error=f"Ollama send failed: {exc}",
                    used_provider=self.provider_id,
                )

    async def list_models(self) -> List[Dict[str, Any]]:
        """List available models from the Ollama server."""
        try:
            base_url = await self._get_base_url()
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(f"{base_url}/api/tags")
                if resp.status_code == 200:
                    data = resp.json()
                    models = data.get("models", [])
                    return [
                        {
                            "name": m.get("name", ""),
                            "size": m.get("size", 0),
                            "digest": m.get("digest", ""),
                            "modified_at": m.get("modified_at", ""),
                        }
                        for m in models
                    ]
        except Exception:  # noqa: BLE001
            pass
        return []

    async def upload_attachment(self, file_path: str) -> Optional[str]:
        """Ollama doesn't support direct file attachments via chat API."""
        logger.warning("Ollama does not support direct file attachments.")
        return None

    async def delete_session(self, session_id: str) -> bool:
        """Ollama sessions are stateless; nothing to delete server-side."""
        logger.info(f"Ollama session deletion is stateless: {session_id}")
        return True

    def get_credentials(self) -> Dict[str, Any]:
        """Return provider-specific credentials for diagnostics."""
        return {
            "provider": self.provider_id,
            "base_url": self._base_url or DEFAULT_OLLAMA_URL,
            "default_model": self._default_model or self.config.default_model,
        }


# Module-level singleton
ollama_provider = OllamaProvider(
    ProviderConfig(
        provider_id=PROVIDER_OLLAMA,
        display_name="Ollama (Local)",
        supports_attachments=False,
        supports_reasoning=False,
        max_files=0,
        max_size_mb=0,
        context_window_tokens=8192,
        default_model="llama3.2:3b",
        available_models=[],
        rate_limit_requests=0,
        rate_limit_window_seconds=60,
        min_interval_seconds=0.0,
        test_model="llama3.2:3b",
        test_model_type="chat",
        modalities=("language", "embedding"),
    )
)
