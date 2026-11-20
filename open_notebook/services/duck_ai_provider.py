"""
Duck.ai provider implementation.

Supports two integration methods:
1. p2d-duck (PyPI sync client) — auto-retry on challenge failures, no API key
2. OpenAI-compatible wrapper server (DUCK_AI_URL) — e.g. pavnxet/duckai-api or amirkabiri/duckai

No Playwright/CDP — lightweight wrappers only.

6 models supported: gpt-6-luna (default), claude-haiku-5.5, gpt-5.4-mini,
mistral-small-4, gpt-oss-120b, gemma-4-31b.

Session series continuation: Duck.ai is anonymous (no retention on browser close),
so ON++ uses session series — new continuation sessions when context is full.
"""

from __future__ import annotations

import asyncio
import importlib.util
from typing import Any, Dict, List, Optional

import httpx
from loguru import logger

from open_notebook.ai.models import ProcessingFlags
from open_notebook.ai.providers import (
    BaseProvider,
    ProviderConfig,
    ProviderSendResult,
    PROVIDER_DUCK_AI,
    DEFAULT_DUCK_AI_MODELS,
    DEFAULT_DUCK_AI_MODEL,
)


class DuckAIProvider(BaseProvider):
    """Duck.ai provider: lightweight wrapper via p2d-duck or OpenAI-compatible server."""

    def __init__(self, config: ProviderConfig):
        super().__init__(PROVIDER_DUCK_AI, config)

    async def is_available(self) -> bool:
        """Duck.ai is available if p2d-duck/duck_ai is installed or DUCK_AI_URL is configured."""
        if importlib.util.find_spec("p2d_duck") is not None or importlib.util.find_spec("duck_ai") is not None:
            return True
        flags = await ProcessingFlags.get_instance()
        return bool(flags.duck_ai_url)

    async def send_message(
        self,
        message: str,
        *,
        session_id: Optional[str] = None,
        thinking_enabled: bool = False,
        ref_file_ids: Optional[List[str]] = None,
        timeout: float = 120.0,
        model: Optional[str] = None,
    ) -> ProviderSendResult:
        """Send a message to Duck.ai via p2d-duck or OpenAI-compatible wrapper."""
        model_name = model or await self._get_default_model()

        # Try p2d-duck/duck_ai first (sync client, run in thread)
        if importlib.util.find_spec("p2d_duck") is not None or importlib.util.find_spec("duck_ai") is not None:
            try:
                response_text = await asyncio.to_thread(
                    self._p2d_duck_send, message, model_name
                )
                return ProviderSendResult(
                    ok=True,
                    response=response_text,
                    session_id=session_id,
                    used_provider=self.provider_id,
                    ref_file_ids=ref_file_ids or [],
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"p2d-duck failed: {exc}")

        # Fall back to OpenAI-compatible wrapper server
        flags = await ProcessingFlags.get_instance()
        duck_ai_url = flags.duck_ai_url
        if duck_ai_url:
            try:
                payload: Dict[str, Any] = {
                    "model": model_name,
                    "messages": [{"role": "user", "content": message}],
                }
                base = duck_ai_url.rstrip("/")
                # Duck.ai proxy servers (e.g. pavnxet/duckai-api) expose
                # /v1/chat/completions directly.  If the URL already ends
                # with /v1, don't double-append it.
                endpoint = f"{base}/chat/completions" if base.endswith("/v1") else f"{base}/v1/chat/completions"
                async with httpx.AsyncClient(timeout=timeout) as client:
                    resp = await client.post(
                        endpoint,
                        json=payload,
                    )
                if resp.status_code == 200:
                    data = resp.json()
                    choices = data.get("choices", [])
                    if choices:
                        message_data = choices[0].get("message", {})
                        return ProviderSendResult(
                            ok=True,
                            response=message_data.get("content", ""),
                            session_id=session_id,
                            used_provider=self.provider_id,
                            ref_file_ids=ref_file_ids or [],
                        )
                if resp.status_code == 429:
                    return ProviderSendResult(
                        ok=False,
                        error="Duck.ai rate limited. Wait for quota reset.",
                        rate_limited=True,
                        used_provider=self.provider_id,
                    )
                return ProviderSendResult(
                    ok=False,
                    error=f"Duck.ai server returned HTTP {resp.status_code}",
                    used_provider=self.provider_id,
                )
            except Exception as exc:  # noqa: BLE001
                logger.error(f"Duck.ai server request failed: {exc}")
                return ProviderSendResult(
                    ok=False,
                    error=f"Duck.ai request failed: {exc}",
                    used_provider=self.provider_id,
                )

        return ProviderSendResult(
            ok=False,
            error=(
                "Duck.ai unavailable: p2d-duck not installed and no "
                "DUCK_AI_URL server configured. Install p2d-duck or set "
                "DUCK_AI_URL in Settings."
            ),
            used_provider=self.provider_id,
        )

    @staticmethod
    def _p2d_duck_send(prompt: str, model: Optional[str]) -> str:
        """Send via p2d-duck/duck_ai sync client (runs in a thread)."""
        try:
            import p2d_duck  # type: ignore
            client = p2d_duck.Client()
            if model:
                response = client.chat(prompt, model=model)
            else:
                response = client.chat(prompt)
        except ImportError:
            import duck_ai as duck_ai_module  # type: ignore
            client = duck_ai_module.DuckChat()
            if model:
                response = client.ask(prompt, model=model)
            else:
                response = client.ask(prompt)
        return response.text if hasattr(response, "text") else str(response)

    async def _get_default_model(self) -> str:
        """Get the default Duck.ai model from ProcessingFlags."""
        flags = await ProcessingFlags.get_instance()
        return flags.duck_ai_default_model or DEFAULT_DUCK_AI_MODEL

    async def upload_attachment(self, file_path: str) -> Optional[str]:
        """Duck.ai supports images and PDFs only via vision-capable models.
        Direct upload not yet implemented via wrapper — use ON++ preprocessor.
        """
        logger.warning(
            "Duck.ai attachment upload not yet implemented via wrapper. "
            "Use ON++ preprocessor for context feeding."
        )
        return None

    async def delete_session(self, session_id: str) -> bool:
        """Delete a Duck.ai session (best effort — anonymous, no server-side delete)."""
        logger.info(f"Duck.ai session deletion is automatic (anonymous): {session_id}")
        return True

    def get_credentials(self) -> Dict[str, Any]:
        """Return provider-specific credentials for diagnostics."""
        flags_check = __import__(
            "open_notebook.ai.models", fromlist=["ProcessingFlags"]
        ).ProcessingFlags
        return {
            "provider": self.provider_id,
            "p2d_duck_available": importlib.util.find_spec("p2d_duck") is not None or importlib.util.find_spec("duck_ai") is not None,
            "has_duck_ai_url": False,  # Would need flags instance; simplified
        }