"""
DeepSeek-web provider implementation.

Wraps deepseek_direct_service in the BaseProvider interface.
Uses direct API calls with session cookies + PoW solving.
Falls back to PC browser-use automation when PoW solving fails locally.
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
    """DeepSeek-web provider: direct API calls with session cookies + PoW solving.

    Falls back to PC browser-use automation when PoW solving fails locally
    (e.g. on phone/ARM devices without wasmtime).
    """

    def __init__(self, config: ProviderConfig):
        super().__init__(PROVIDER_DEEPSEEK_WEB, config)
        self._credentials_configured = False

    async def is_available(self) -> bool:
        """DeepSeek-web is available if credentials are configured AND
        either PoW solver is installed OR PC fallback URL is set."""
        flags = await ProcessingFlags.get_instance()
        has_creds = bool(
            flags.deepseek_ds_session_id and flags.deepseek_authorization_token
        )
        if not has_creds:
            return False
        has_solver = pow_solver_available()
        has_pc_fallback = bool(flags.deepseek_pc_url)
        return has_solver or has_pc_fallback

    def _configure_from_flags(self, flags: ProcessingFlags) -> None:
        """Configure the direct service with stored credentials."""
        if flags.deepseek_ds_session_id and flags.deepseek_authorization_token:
            configure_direct(
                flags.deepseek_ds_session_id, flags.deepseek_authorization_token
            )
            self._credentials_configured = True

    async def _run_pc_browser_use(
        self,
        message: str,
        thinking_enabled: bool,
        pc_url: Optional[str] = None,
        session_id: Optional[str] = None,
        notebook_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Run browser-use for DeepSeek-web fallback.

        With ``pc_url`` set, forward the request to the PC's API.
        Without it, run browser-use locally.
        """
        import httpx

        if not pc_url:
            from open_notebook.services.deepseek_web_service import (
                deepseek_web_service,
            )
            result = await asyncio.to_thread(
                deepseek_web_service.run_deepseek,
                message,
                True,
                180.0,
            )
            return {
                "response": result.get("response"),
                "thinking": result.get("thinking"),
                "error": result.get("error"),
                "manual_login_required": result.get("manual_login_required", False),
                "rate_limited": result.get("rate_limited", False),
            }

        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(180.0)) as client:
                resp = await client.post(
                    f"{pc_url.rstrip('/')}/api/chat/deepseek-web/pc-fallback",
                    json={
                        "message": message,
                        "thinking_enabled": thinking_enabled,
                        "session_id": session_id,
                        "notebook_id": notebook_id,
                    },
                )
                if resp.status_code == 200:
                    data = resp.json()
                    return {
                        "response": data.get("response"),
                        "thinking": data.get("thinking"),
                        "error": data.get("error"),
                        "manual_login_required": data.get("manual_login_required", False),
                        "rate_limited": data.get("rate_limited", False),
                    }
                else:
                    return {
                        "response": None,
                        "error": f"PC fallback returned HTTP {resp.status_code}: {resp.text[:200]}",
                        "manual_login_required": False,
                        "rate_limited": False,
                    }
        except Exception as exc:  # noqa: BLE001
            logger.error(f"PC browser-use fallback error: {exc}")
            return {
                "response": None,
                "error": str(exc),
                "manual_login_required": False,
                "rate_limited": False,
            }

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
        """Send a message to DeepSeek-web via direct API calls.

        If PoW solving fails locally and pc_fallback=True, delegates to
        browser-use on a PC (x86 machine with Chromium).
        """
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

        # Proactive rate limit check to avoid client bans / ToS violations
        rate_limit_error = await self._check_rate_limit()
        if rate_limit_error:
            return ProviderSendResult(
                ok=False,
                error=rate_limit_error,
                rate_limited=True,
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

        # If PoW required and not solved, try PC fallback
        if result.get("pow_required") and not result.get("pow_solved"):
            if pc_fallback:
                logger.info("PoW required, delegating to PC browser-use fallback")
                pc_result = await self._run_pc_browser_use(
                    message=transformed_prompt,
                    thinking_enabled=thinking_enabled,
                    pc_url=flags.deepseek_pc_url,
                    session_id=session_id,
                    notebook_id=None,
                )
                if pc_result.get("error"):
                    return ProviderSendResult(
                        ok=False,
                        error=f"PC browser-use fallback failed: {pc_result['error']}",
                        used_provider=self.provider_id,
                    )
                return ProviderSendResult(
                    ok=True,
                    response=pc_result.get("response"),
                    thinking=pc_result.get("thinking"),
                    session_id=pc_result.get("session_id"),
                    used_provider=self.provider_id,
                    ref_file_ids=ref_file_ids or [],
                    pow_required=True,
                    pow_solved=False,
                    used_pc_fallback=True,
                )
            else:
                return ProviderSendResult(
                    ok=False,
                    error=(
                        "DeepSeek-web issued a Proof-of-Work challenge that "
                        "could not be solved. Install the solver on this "
                        "device (pip install deepseek-pow wasmtime), enable "
                        "pc_fallback to use browser-use on the PC, or "
                        "re-log in to chat.deepseek.com and refresh credentials."
                    ),
                    pow_required=True,
                    pow_solved=False,
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