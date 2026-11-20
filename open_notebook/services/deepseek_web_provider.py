"""
DeepSeek-web provider implementation.

Wraps deepseek_direct_service in the BaseProvider interface.
No Playwright/CDP — uses direct API calls with session cookies + PoW solving.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

from loguru import logger

from open_notebook.ai.providers import (
    BaseProvider,
    ProviderConfig,
    ProviderSendResult,
    PROVIDER_DEEPSEEK_WEB,
)
from open_notebook.ai.models import ProcessingFlags
from open_notebook.services.deepseek_direct_service import (
    configure as configure_direct,
    deepseek_direct_service,
    pow_solver_available,
)
from open_notebook.services.prompter_service import prompter_service


class DeepSeekWebProvider(BaseProvider):
    """DeepSeek-web provider: direct API calls with session cookies + PoW solving."""

    def __init__(self, config: ProviderConfig):
        super().__init__(PROVIDER_DEEPSEEK_WEB, config)
        self._credentials_configured = False

    async def is_available(self) -> bool:
        """DeepSeek-web is available if credentials are configured and PoW solver is installed."""
        flags = await ProcessingFlags.get_instance()
        has_creds = bool(
            flags.deepseek_ds_session_id and flags.deepseek_authorization_token
        )
        has_solver = pow_solver_available()
        return has_creds and has_solver

    def _configure_from_flags(self, flags: ProcessingFlags) -> None:
        """Configure the direct service with stored credentials."""
        if flags.deepseek_ds_session_id and flags.deepseek_authorization_token:
            configure_direct(
                flags.deepseek_ds_session_id, flags.deepseek_authorization_token
            )
            self._credentials_configured = True

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
        """Send a message to DeepSeek-web via direct API calls."""
        flags = await ProcessingFlags.get_instance()
        self._configure_from_flags(flags)

        if not flags.deepseek_ds_session_id or not flags.deepseek_authorization_token:
            return ProviderSendResult(
                ok=False,
                error=(
                    "DeepSeek-web credentials not configured. "
                    "Log in to chat.deepseek.com in a browser, copy the "
                    "ds_session_id cookie and authorization Bearer token "
                    "from DevTools Network tab, and configure them in Settings."
                ),
                manual_login_required=True,
                used_provider=self.provider_id,
            )

        if not pow_solver_available():
            return ProviderSendResult(
                ok=False,
                error=(
                    "DeepSeek PoW solver unavailable on this device. "
                    "Install deepseek-pow + wasmtime, or use a different model."
                ),
                used_provider=self.provider_id,
            )

        # Transform message through prompter (session-aware).
        transformed_prompt = await prompter_service.transform(
            raw_message=message,
            session_id=session_id,
            notebook_id=None,
            message_type="chat",
        )

        # Run in thread since deepseek_direct_service.send_message is synchronous.
        loop = asyncio.get_event_loop()
        try:
            result = await loop.run_in_executor(
                None,
                lambda: deepseek_direct_service.send_message(
                    message=transformed_prompt,
                    thinking_enabled=thinking_enabled,
                    timeout=timeout,
                    ref_file_ids=ref_file_ids,
                    chat_session_id=None,  # Always fresh session to avoid stale IDs
                ),
            )
        except Exception as exc:
            logger.error(f"DeepSeek direct send failed: {exc}")
            return ProviderSendResult(
                ok=False,
                error=f"DeepSeek-web send failed: {exc}",
                used_provider=self.provider_id,
            )

        if result.get("manual_login_required"):
            return ProviderSendResult(
                ok=False,
                error=(
                    "DeepSeek-web session expired. Manual login required: "
                    "open chat.deepseek.com in a browser, log in, and the session "
                    "cookies will be persisted for future requests."
                ),
                manual_login_required=True,
                used_provider=self.provider_id,
            )

        if result.get("rate_limited"):
            return ProviderSendResult(
                ok=False,
                error="DeepSeek-web rate limited. Wait for quota reset.",
                rate_limited=True,
                used_provider=self.provider_id,
            )

        if result.get("error"):
            return ProviderSendResult(
                ok=False,
                error=f"DeepSeek-web error: {result['error']}",
                used_provider=self.provider_id,
            )

        return ProviderSendResult(
            ok=True,
            response=result.get("response"),
            thinking=result.get("thinking"),
            session_id=result.get("session_id"),
            used_provider=self.provider_id,
            ref_file_ids=ref_file_ids or [],
        )

    async def upload_attachment(self, file_path: str) -> Optional[str]:
        """Upload a file to DeepSeek-web and return its ref_file_id."""
        try:
            file_id = deepseek_direct_service.upload_file(file_path)
            return file_id
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"DeepSeek attachment upload failed: {exc}")
            return None

    async def delete_session(self, session_id: str) -> bool:
        """Delete a DeepSeek-web session (best effort)."""
        # DeepSeek-web doesn't have a direct delete API via direct service.
        # Session deletion is handled by the user in the DeepSeek web UI.
        logger.info(f"DeepSeek-web session deletion is manual (web UI): {session_id}")
        return True

    def get_credentials(self) -> Dict[str, Any]:
        """Return provider-specific credentials for diagnostics."""
        return {
            "provider": self.provider_id,
            "has_credentials": self._credentials_configured,
        }