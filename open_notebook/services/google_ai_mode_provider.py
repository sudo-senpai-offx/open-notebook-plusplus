"""
Google AI Mode (Gemini 4 Hydrogen) provider implementation.

Uses a lightweight OpenAI-compatible wrapper (XD06/google-ai-mode or similar)
exposed at GOOGLE_AI_MODE_URL. No Playwright/CDP — httpx fast path only.

Context feeding: Google AI Mode has a sliding window context (32K free tier),
so older turns drop out when the limit is exceeded. ON++ preprocesses and feeds
full context via continuation prompts when switching to/from this provider.
"""

from __future__ import annotations

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
    """Google AI Mode provider: lightweight OpenAI-compatible wrapper via httpx."""

    def __init__(self, config: ProviderConfig):
        super().__init__(PROVIDER_GOOGLE_AI_MODE, config)

    async def is_available(self) -> bool:
        """Google AI Mode is available if GOOGLE_AI_MODE_URL is configured and reachable."""
        flags = await ProcessingFlags.get_instance()
        url = flags.google_ai_mode_url
        if not url:
            return False
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(f"{url.rstrip('/')}/health")
                return resp.status_code == 200
        except Exception:  # noqa: BLE001
            return False

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
        """Send a message to Google AI Mode via the local wrapper API."""
        flags = await ProcessingFlags.get_instance()
        ai_mode_url = flags.google_ai_mode_url
        if not ai_mode_url:
            return ProviderSendResult(
                ok=False,
                error=(
                    "Google AI Mode not configured. "
                    "Set GOOGLE_AI_MODE_URL in Settings to point to your "
                    "local OpenAI-compatible wrapper (e.g. XD06/google-ai-mode)."
                ),
                used_provider=self.provider_id,
            )

        # Build payload for the wrapper endpoint.
        # The wrapper (XD06/google-ai-mode) exposes POST /v1/web/ai-mode
        # with {prompt, country, language, session_token}.
        payload: Dict[str, Any] = {
            "prompt": message,
            "country": "us",
            "language": "en",
        }
        if session_id:
            payload["session_token"] = session_id

        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(
                    f"{ai_mode_url.rstrip('/')}/v1/web/ai-mode",
                    json=payload,
                )
        except Exception as exc:  # noqa: BLE001
            logger.error(f"Google AI Mode request failed: {exc}")
            return ProviderSendResult(
                ok=False,
                error=f"Google AI Mode request failed: {exc}",
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
                error=f"Google AI Mode API returned HTTP {resp.status_code}",
                used_provider=self.provider_id,
            )

        try:
            data = resp.json()
        except Exception as exc:  # noqa: BLE001
            return ProviderSendResult(
                ok=False,
                error=f"Google AI Mode invalid response: {exc}",
                used_provider=self.provider_id,
            )

        reply_parts = data.get("reply_parts", [])
        response_text = self._extract_reply_parts(reply_parts)
        new_session = data.get("session_token") or session_id

        if not response_text:
            return ProviderSendResult(
                ok=False,
                error="Empty response from Google AI Mode.",
                used_provider=self.provider_id,
            )

        return ProviderSendResult(
            ok=True,
            response=response_text,
            session_id=new_session,
            used_provider=self.provider_id,
            ref_file_ids=ref_file_ids or [],
        )

    @staticmethod
    def _extract_reply_parts(reply_parts: List[Dict[str, Any]]) -> str:
        """Extract text from Google AI Mode reply_parts."""
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

    async def upload_attachment(self, file_path: str) -> Optional[str]:
        """Google AI Mode doesn't support direct file upload via this wrapper.
        Attachments are handled by ON++ preprocessor (text extraction + context feeding).
        """
        logger.warning(
            "Google AI Mode does not support direct file upload via wrapper. "
            "Use ON++ preprocessor for context feeding."
        )
        return None

    async def delete_session(self, session_id: str) -> bool:
        """Delete a Google AI Mode session (best effort — anonymous, no server-side delete)."""
        # Google AI Mode is anonymous; sessions are lost when browser closes.
        # No server-side delete needed.
        logger.info(f"Google AI Mode session deletion is automatic (anonymous): {session_id}")
        return True

    def get_credentials(self) -> Dict[str, Any]:
        """Return provider-specific credentials for diagnostics."""
        return {
            "provider": self.provider_id,
            "has_url": bool(
                __import__("open_notebook.ai.models", fromlist=["ProcessingFlags"])
                .ProcessingFlags
            ),
        }