"""
Google AI Mode (Gemini 4 Hydrogen) provider implementation.

Supports three integration methods, tried in order:

1. **Local wrapper URL** (GOOGLE_AI_MODE_URL) — points to a lightweight
   OpenAI-compatible wrapper (e.g. XD06/google-ai-mode). No Playwright/CDP.
2. **Lightpanda CDP server** (LIGHTPANDA_CDP_URL) — Zig-based headless
   browser (~123MB RAM vs Chrome's 2GB+) that speaks CDP. Playwright connects
   via connect_over_cdp(). No API key needed — uses your own browser session.
   Run: lightpanda serve --host 127.0.0.1 --port 9222
3. **Direct Gemini API** (GEMINI_API_KEY) — uses Google's official
   OpenAI-compatible endpoint at
   https://generativelanguage.googleapis.com/v1beta/openai/
   Requires a free Gemini API key from Google AI Studio.

Context feeding: Google AI Mode has a sliding window context (32K free tier),
so older turns drop out when the limit is exceeded. ON++ preprocesses and feeds
full context via continuation prompts when switching to/from this provider.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any, Dict, List, Optional

import httpx
from loguru import logger

from open_notebook.ai.models import ProcessingFlags
from open_notebook.ai.providers import (
    BaseProvider,
    ProviderConfig,
    ProviderSendResult,
    PROVIDER_GOOGLE_AI_MODE,
)


class GoogleAIModeProvider(BaseProvider):
    """Google AI Mode provider: lightweight access via wrapper URL,
    Lightpanda CDP server, or direct Gemini API."""

    def __init__(self, config: ProviderConfig):
        super().__init__(PROVIDER_GOOGLE_AI_MODE, config)

    # ------------------------------------------------------------------
    # Configuration helpers
    # ------------------------------------------------------------------

    async def _get_flags(self) -> ProcessingFlags:
        """Get ProcessingFlags instance (async)."""
        from open_notebook.ai.models import ProcessingFlags as PF  # noqa: PLC0415
        return await PF.get_instance()

    async def _gemini_api_key(self) -> Optional[str]:
        return os.getenv("GEMINI_API_KEY") or (await self._get_flags()).gemini_api_key

    def _gemini_base_url(self) -> str:
        return os.getenv(
            "GEMINI_API_BASE_URL",
            "https://generativelanguage.googleapis.com/v1beta/openai",
        )

    async def _wrapper_url(self) -> Optional[str]:
        return (await self._get_flags()).google_ai_mode_url

    async def _lightpanda_cdp_url(self) -> Optional[str]:
        return (await self._get_flags()).lightpanda_cdp_url

    # ------------------------------------------------------------------
    # Availability
    # ------------------------------------------------------------------

    async def is_available(self) -> bool:
        """Google AI Mode is available if any of:
        - A wrapper URL is configured and reachable
        - A Lightpanda CDP server is running and reachable
        - A Gemini API key is set
        """
        flags = await self._get_flags()

        # Option 1: Wrapper URL
        if flags.google_ai_mode_url:
            try:
                async with httpx.AsyncClient(timeout=5.0) as client:
                    resp = await client.get(
                        f"{flags.google_ai_mode_url.rstrip('/')}/health"
                    )
                    if resp.status_code == 200:
                        return True
            except Exception:  # noqa: BLE001
                pass

        # Option 2: Lightpanda CDP server
        if flags.lightpanda_cdp_url:
            try:
                async with httpx.AsyncClient(timeout=5.0) as client:
                    resp = await client.get(
                        f"{flags.lightpanda_cdp_url.rstrip('/')}/json/version"
                    )
                    if resp.status_code == 200:
                        return True
            except Exception:  # noqa: BLE001
                pass

        # Option 3: Direct Gemini API
        return bool(await self._gemini_api_key())

    # ------------------------------------------------------------------
    # Send message
    # ------------------------------------------------------------------

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
        """Send a message to Google AI Mode.

        Tries methods in order:
        1. Local wrapper URL (GOOGLE_AI_MODE_URL)
        2. Lightpanda CDP server (LIGHTPANDA_CDP_URL)
        3. Direct Gemini API (GEMINI_API_KEY)
        """
        # Proactive rate limit check to avoid client bans / ToS violations
        rate_limit_error = await self._check_rate_limit()
        if rate_limit_error:
            return ProviderSendResult(
                ok=False,
                error=rate_limit_error,
                rate_limited=True,
                used_provider=self.provider_id,
            )
        # --- Option 1: Local wrapper URL ---
        wrapper_url = await self._wrapper_url()
        if wrapper_url:
            return await self._send_via_wrapper(
                wrapper_url, message, session_id, thinking_enabled,
                ref_file_ids, timeout,
            )

        # --- Option 2: Lightpanda CDP server ---
        lightpanda_url = await self._lightpanda_cdp_url()
        if lightpanda_url:
            return await self._send_via_lightpanda_cdp(
                lightpanda_url, message, session_id, thinking_enabled,
                ref_file_ids, timeout,
            )

        # --- Option 3: Direct Gemini API ---
        api_key = await self._gemini_api_key()
        if not api_key:
            return ProviderSendResult(
                ok=False,
                error=(
                    "Google AI Mode not configured. "
                    "Set GOOGLE_AI_MODE_URL to point to a local wrapper, "
                    "set LIGHTPANDA_CDP_URL to a Lightpanda CDP server, "
                    "or set GEMINI_API_KEY to use the Gemini API directly."
                ),
                used_provider=self.provider_id,
            )

        return await self._send_via_gemini_api(
            api_key, message, session_id, thinking_enabled, ref_file_ids, timeout, model
        )

    # ------------------------------------------------------------------
    # Wrapper path (XD06/google-ai-mode or similar)
    # ------------------------------------------------------------------

    async def _send_via_wrapper(
        self,
        ai_mode_url: str,
        message: str,
        session_id: Optional[str],
        thinking_enabled: bool,
        ref_file_ids: Optional[List[str]],
        timeout: float,
    ) -> ProviderSendResult:
        """Send via XD06/google-ai-mode OpenAI-compatible wrapper at /v1/chat/completions."""
        model_name = "google-ai-mode"
        payload: Dict[str, Any] = {
            "model": model_name,
            "messages": [{"role": "user", "content": message}],
            "stream": False,
        }
        # Google AI Mode ignores system role; only last user message is used.
        # Session continuation is handled by passing full message history.

        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                base = ai_mode_url.rstrip("/")
                # XD06/google-ai-mode exposes /v1/chat/completions directly.
                # If the URL already ends with /v1, don't double-append it.
                endpoint = (
                    f"{base}/chat/completions"
                    if base.endswith("/v1")
                    else f"{base}/v1/chat/completions"
                )
                resp = await client.post(
                    endpoint,
                    json=payload,
                )
        except Exception as exc:  # noqa: BLE001
            logger.error(f"Google AI Mode wrapper request failed: {exc}")
            return ProviderSendResult(
                ok=False,
                error=f"Google AI Mode wrapper request failed: {exc}",
                used_provider=self.provider_id,
            )

        if resp.status_code == 401:
            return ProviderSendResult(
                ok=False,
                error="Google AI Mode wrapper: cookies expired. Re-export cookies from browser.",
                manual_login_required=True,
                used_provider=self.provider_id,
            )

        if resp.status_code == 429:
            return ProviderSendResult(
                ok=False,
                error="Google AI Mode rate limited. Wait for quota reset.",
                rate_limited=True,
                used_provider=self.provider_id,
            )

        if resp.status_code != 200:
            return ProviderSendResult(
                ok=False,
                error=f"Google AI Mode wrapper returned HTTP {resp.status_code}: {resp.text[:200]}",
                used_provider=self.provider_id,
            )

        try:
            data = resp.json()
        except Exception as exc:  # noqa: BLE001
            return ProviderSendResult(
                ok=False,
                error=f"Google AI Mode wrapper invalid response: {exc}",
                used_provider=self.provider_id,
            )

        choices = data.get("choices", [])
        if not choices:
            return ProviderSendResult(
                ok=False,
                error="Google AI Mode wrapper returned no choices.",
                used_provider=self.provider_id,
            )

        message_data = choices[0].get("message", {})
        response_text = message_data.get("content", "")

        if not response_text:
            return ProviderSendResult(
                ok=False,
                error="Empty response from Google AI Mode wrapper.",
                used_provider=self.provider_id,
            )

        return ProviderSendResult(
            ok=True,
            response=response_text,
            session_id=session_id,
            used_provider=self.provider_id,
            ref_file_ids=ref_file_ids or [],
        )

    # ------------------------------------------------------------------
    # Lightpanda CDP path (Lightpanda Python SDK)
    # ------------------------------------------------------------------

    async def _send_via_lightpanda_cdp(
        self,
        cdp_url: str,
        message: str,
        session_id: Optional[str],
        thinking_enabled: bool,
        ref_file_ids: Optional[List[str]],
        timeout: float,
    ) -> ProviderSendResult:
        """Send via Lightpanda browser using the Python SDK.

        Lightpanda is a Zig-based headless browser (~123MB RAM vs Chrome's 2GB+)
        that speaks CDP. It executes JavaScript natively, so Google sees a real
        browser TLS fingerprint, bypassing the SG_REL soft-block that
        curl_cffi-based wrappers hit.

        No Playwright binary download needed — Lightpanda brings its own
        binary. Run: lightpanda serve --host 127.0.0.1 --port 9222
        Then connect via this SDK.
        """
        from lightpanda import Browser

        try:
            # Lightpanda spawns its own binary; connect to the CDP server
            # if one is already running, otherwise start a fresh one.
            browser = Browser(
                timeout=timeout / 1000.0,  # Lightpanda uses seconds
                verbose=False,
            )
            session = browser.new_session()

            # Navigate to Google AI Mode
            result = session.goto(
                url="https://www.google.com/search?udm=50&hl=en",
                wait_until="networkidle",
                timeout=int(timeout * 1000),
            )

            if result.http_status and result.http_status >= 400:
                session.close()
                browser.close()
                return ProviderSendResult(
                    ok=False,
                    error=f"Google AI Mode returned HTTP {result.http_status}",
                    used_provider=self.provider_id,
                )

            # Fill the search box and submit
            session.fill(
                value=message,
                selector='input[name="q"]',
            )
            session.press(key="Enter", selector='input[name="q"]')

            # Wait for AI Mode response to appear
            session.wait_for_selector(
                selector=".n6owBd, .T286Pc, [data-mstk]",
                timeout=int(timeout * 1000),
            )

            # Extract response as markdown
            response_text = session.markdown() or ""

            session.close()
            browser.close()

            if not response_text or not response_text.strip():
                return ProviderSendResult(
                    ok=False,
                    error="Empty response from Google AI Mode via Lightpanda.",
                    used_provider=self.provider_id,
                )

            return ProviderSendResult(
                ok=True,
                response=response_text.strip(),
                session_id=session_id,
                used_provider=self.provider_id,
                ref_file_ids=ref_file_ids or [],
            )

        except ImportError:
            logger.error("lightpanda package not installed. Install with: pip install lightpanda")
            return ProviderSendResult(
                ok=False,
                error=(
                    "Lightpanda CDP requires the lightpanda package. "
                    "Install with: pip install lightpanda"
                ),
                used_provider=self.provider_id,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(f"Lightpanda request failed: {exc}")
            return ProviderSendResult(
                ok=False,
                error=f"Lightpanda request failed: {exc}",
                used_provider=self.provider_id,
            )

    # ------------------------------------------------------------------
    # Direct Gemini API path (OpenAI-compatible)
    # ------------------------------------------------------------------

    async def _send_via_gemini_api(
        self,
        api_key: str,
        message: str,
        session_id: Optional[str],
        thinking_enabled: bool,
        ref_file_ids: Optional[List[str]],
        timeout: float,
        model: Optional[str],
    ) -> ProviderSendResult:
        """Send a message via the Gemini API OpenAI-compatible endpoint."""
        base_url = self._gemini_base_url().rstrip("/")
        model_name = model or "gemini-4-hydrogen"

        payload: Dict[str, Any] = {
            "model": model_name,
            "messages": [{"role": "user", "content": message}],
            "temperature": 0.7,
            "max_tokens": 4096,
        }
        if session_id:
            # Gemini doesn't have native session IDs; we pass it as metadata
            payload["metadata"] = {"session_id": session_id}

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(
                    f"{base_url}/chat/completions",
                    json=payload,
                    headers=headers,
                )
        except Exception as exc:  # noqa: BLE001
            logger.error(f"Gemini API request failed: {exc}")
            return ProviderSendResult(
                ok=False,
                error=f"Gemini API request failed: {exc}",
                used_provider=self.provider_id,
            )

        if resp.status_code == 429:
            return ProviderSendResult(
                ok=False,
                error="Gemini API rate limited. Wait for quota reset.",
                rate_limited=True,
                used_provider=self.provider_id,
            )

        if resp.status_code == 401:
            return ProviderSendResult(
                ok=False,
                error="Gemini API invalid API key. Check GEMINI_API_KEY.",
                manual_login_required=True,
                used_provider=self.provider_id,
            )

        if resp.status_code != 200:
            return ProviderSendResult(
                ok=False,
                error=f"Gemini API returned HTTP {resp.status_code}: {resp.text[:200]}",
                used_provider=self.provider_id,
            )

        try:
            data = resp.json()
        except Exception as exc:  # noqa: BLE001
            return ProviderSendResult(
                ok=False,
                error=f"Gemini API invalid response: {exc}",
                used_provider=self.provider_id,
            )

        choices = data.get("choices", [])
        if not choices:
            return ProviderSendResult(
                ok=False,
                error="Gemini API returned no choices.",
                used_provider=self.provider_id,
            )

        message_data = choices[0].get("message", {})
        response_text = message_data.get("content", "")

        if not response_text:
            return ProviderSendResult(
                ok=False,
                error="Empty response from Gemini API.",
                used_provider=self.provider_id,
            )

        return ProviderSendResult(
            ok=True,
            response=response_text,
            session_id=session_id,
            used_provider=self.provider_id,
            ref_file_ids=ref_file_ids or [],
        )

    # ------------------------------------------------------------------
    # Attachments / session
    # ------------------------------------------------------------------

    async def upload_attachment(self, file_path: str) -> Optional[str]:
        """Google AI Mode doesn't support direct file upload via wrapper/CDP.
        Attachments are handled by ON++ preprocessor (text extraction +
        context feeding).
        """
        logger.warning(
            "Google AI Mode does not support direct file upload via wrapper/CDP. "
            "Use ON++ preprocessor for context feeding."
        )
        return None

    async def delete_session(self, session_id: str) -> bool:
        """Delete a Google AI Mode session (best effort — anonymous,
        no server-side delete)."""
        logger.info(
            f"Google AI Mode session deletion is automatic (anonymous): {session_id}"
        )
        return True

    async def get_credentials(self) -> Dict[str, Any]:
        """Return provider-specific credentials for diagnostics."""
        flags = await self._get_flags()
        return {
            "provider": self.provider_id,
            "has_wrapper_url": bool(flags.google_ai_mode_url),
            "has_lightpanda_cdp_url": bool(flags.lightpanda_cdp_url),
            "has_api_key": bool(await self._gemini_api_key()),
        }

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_reply_parts(reply_parts: List[Dict[str, Any]]) -> str:
        """Extract text from Google AI Mode wrapper reply_parts."""
        parts = []
        for part in reply_parts:
            ptype = part.get("type", "")
            if ptype == "paragraph":
                parts.append(part.get("text", ""))
            elif ptype == "heading":
                parts.append(f"# {part.get('text', '')}")
            elif ptype == "list":
                items = part.get("items", [])
                for item in items:
                    parts.append(f"- {item}")
        return "\n".join(parts).strip()