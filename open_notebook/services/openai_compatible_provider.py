"""
OpenAI-compatible provider implementation.

Wraps any OpenAI-compatible API endpoint (Groq, NVIDIA NIM, OpenRouter,
SambaNova, etc.) in the BaseProvider interface.

No Playwright/CDP — lightweight httpx wrapper only. Reads API key from
ProcessingFlags fields or Credential table, merges extra_headers from
ProviderConfig (e.g. OpenRouter attribution headers).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import httpx
from loguru import logger

from open_notebook.ai.models import ProcessingFlags
from open_notebook.ai.providers import (
    BaseProvider,
    ProviderConfig,
    ProviderSendResult,
)
from open_notebook.ai.routing import (
    is_provider_at_limit,
    record_provider_request,
)
from open_notebook.domain.credential import Credential
from open_notebook.utils.circuit_breaker import (
    CircuitBreakerOpenError,
    get_circuit_breaker,
)


class OpenAICompatibleProvider(BaseProvider):
    """Generic OpenAI-compatible API provider.

    Supports Groq, NVIDIA NIM, OpenRouter, SambaNova, and any other
    provider that exposes /v1/chat/completions and /v1/models.
    """

    def __init__(self, config: ProviderConfig):
        super().__init__(config.provider_id, config)
        self._api_key: Optional[str] = None

    async def _resolve_api_key(self) -> Optional[str]:
        """Resolve API key from ProcessingFlags dedicated field, then Credential table."""
        if self._api_key:
            return self._api_key

        # Map provider_id to ProcessingFlags field name
        flag_field_map = {
            "groq": "groq_api_key",
            "nvidia": "nvidia_api_key",
            "openrouter": "openrouter_api_key",
            "sambanova": "sambanova_api_key",
        }
        flag_field = flag_field_map.get(self.provider_id)
        if flag_field:
            try:
                flags = await ProcessingFlags.get_instance()
                key = getattr(flags, flag_field, None)
                if key:
                    self._api_key = key
                    return key
            except Exception as e:
                logger.debug(
                    f"Could not read {flag_field} from ProcessingFlags: {e}"
                )

        # Fall back to Credential table
        try:
            creds = await Credential.get_by_provider(self.provider_id)
            if creds and creds[0].api_key:
                self._api_key = creds[0].api_key.get_secret_value()
                return self._api_key
        except Exception as e:
            logger.debug(
                f"Could not load credential for {self.provider_id}: {e}"
            )

        return None

    async def is_available(self) -> bool:
        """Available if an API key is configured."""
        key = await self._resolve_api_key()
        return bool(key)

    def _build_headers(self, api_key: str) -> Dict[str, str]:
        """Build request headers, merging extra_headers from config."""
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        headers.update(self.config.extra_headers)
        return headers

    def _base_url(self) -> str:
        """Resolve base URL from config or ProcessingFlags."""
        base_url_env = self.config.base_url_env
        if base_url_env:
            import os

            env_url = os.environ.get(base_url_env)
            if env_url:
                return env_url.rstrip("/")
        # Default base URLs per provider
        defaults = {
            "groq": "https://api.groq.com/openai/v1",
            "nvidia": "https://integrate.api.nvidia.com/v1",
            "openrouter": "https://openrouter.ai/api/v1",
            "sambanova": "https://api.sambanova.ai/v1",
        }
        return defaults.get(self.provider_id, "").rstrip("/")

    async def _send_message_impl(
        self,
        message: str,
        *,
        session_id: Optional[str] = None,
        thinking_enabled: bool = False,
        ref_file_ids: Optional[List[str]] = None,
        timeout: float = 120.0,
        model: Optional[str] = None,
    ) -> ProviderSendResult:
        """Send a chat completion request to the OpenAI-compatible endpoint."""
        # Proactive rate limit check
        model_name = model or self.config.default_model or ""
        rate_limit_error = await self._check_rate_limit()
        if rate_limit_error:
            return ProviderSendResult(
                ok=False,
                error=rate_limit_error,
                rate_limited=True,
                used_provider=self.provider_id,
            )

        if is_provider_at_limit(self.provider_id, model_name):
            return ProviderSendResult(
                ok=False,
                error=f"Rate limit reached for {self.provider_id}/{model_name}",
                rate_limited=True,
                used_provider=self.provider_id,
            )

        # Circuit breaker: reject calls immediately if provider is failing
        cb = await get_circuit_breaker(
            self.provider_id,
            failure_threshold=5,
            recovery_timeout_seconds=60.0,
        )

        async def _send() -> ProviderSendResult:
            api_key = await self._resolve_api_key()
            if not api_key:
                return ProviderSendResult(
                    ok=False,
                    error=f"No API key configured for {self.provider_id}",
                    used_provider=self.provider_id,
                )

            headers = self._build_headers(api_key)
            url = f"{self._base_url()}/chat/completions"

            payload: Dict[str, Any] = {
                "model": model_name,
                "messages": [{"role": "user", "content": message}],
                "stream": False,
            }

            try:
                async with httpx.AsyncClient(timeout=timeout) as client:
                    response = await client.post(url, headers=headers, json=payload)
                    if response.status_code == 401:
                        return ProviderSendResult(
                            ok=False,
                            error="Invalid API key",
                            used_provider=self.provider_id,
                        )
                    if response.status_code == 429:
                        retry_after = None
                        ra_header = response.headers.get("retry-after")
                        if ra_header:
                            try:
                                retry_after = float(ra_header)
                            except ValueError:
                                # Retry-After can be an HTTP-date, e.g.
                                # "Wed, 21 Oct 2015 07:28:00 GMT". Parse it
                                # into seconds from now so the retry cap
                                # (60s) can be enforced consistently.
                                try:
                                    from email.utils import parsedate_to_datetime

                                    retry_dt = parsedate_to_datetime(ra_header)
                                    if retry_dt is not None:
                                        now = datetime.now(timezone.utc)
                                        delta = (retry_dt - now).total_seconds()
                                        retry_after = max(0.0, delta)
                                except Exception:
                                    # Unparseable — fall back to backoff
                                    retry_after = None
                        return ProviderSendResult(
                            ok=False,
                            error="Provider rate limit exceeded",
                            rate_limited=True,
                            retry_after=retry_after,
                            used_provider=self.provider_id,
                        )
                    response.raise_for_status()
                    data = response.json()

                record_provider_request(self.provider_id, model_name)

                content = ""
                choices = data.get("choices", [])
                if choices:
                    content = choices[0].get("message", {}).get("content", "")

                return ProviderSendResult(
                    ok=True,
                    response=content,
                    used_provider=self.provider_id,
                )
            except httpx.ConnectError:
                return ProviderSendResult(
                    ok=False,
                    error=f"Cannot connect to {self.provider_id}. Check the base URL.",
                    used_provider=self.provider_id,
                )
            except httpx.TimeoutException:
                return ProviderSendResult(
                    ok=False,
                    error=f"Connection to {self.provider_id} timed out.",
                    used_provider=self.provider_id,
                )
            except Exception as e:  # noqa: BLE001
                logger.exception(f"{self.provider_id} send_message failed: {e}")
                return ProviderSendResult(
                    ok=False,
                    error=str(e)[:200],
                    used_provider=self.provider_id,
                )

        try:
            result = await cb.call(_send)
        except CircuitBreakerOpenError:
            return ProviderSendResult(
                ok=False,
                error=f"Provider {self.provider_id} temporarily unavailable (circuit breaker open)",
                rate_limited=True,
                used_provider=self.provider_id,
            )

        return result

    async def upload_attachment(self, file_path: str) -> Optional[str]:
        """OpenAI-compatible providers in this tier do not support attachments."""
        return None

    async def delete_session(self, session_id: str) -> bool:
        """Stateless provider — no session to delete."""
        return True

    def get_credentials(self) -> Dict[str, Any]:
        """Return masked API key status for diagnostics."""
        return {
            "has_api_key": bool(self._api_key),
            "provider": self.provider_id,
            "base_url": self._base_url(),
            "extra_headers": list(self.config.extra_headers.keys()),
        }
