"""DeepSeek-web chat endpoints — browser-use session capture for chat.deepseek.com.

Routes LLM requests through browser automation instead of direct API calls,
allowing free-tier access to DeepSeek's web interface (DeepThink, web search,
multi-file upload) without API keys.

Endpoints:
  POST /api/chat/deepseek-web — Send a message via browser-use, get response.
  GET  /api/chat/deepseek-web/session — Session status (login state, rate limits).
  POST /api/chat/deepseek-web/session/refresh — Refresh session cookies.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException
from loguru import logger
from pydantic import BaseModel, Field

from open_notebook.services.browser_use_service import (
    SESSION_FILE,
    browser_use_service,
)
from open_notebook.services.prompter_service import prompter_service
from open_notebook.services.quota_tracker import quota_tracker

router = APIRouter()


class DeepSeekWebRequest(BaseModel):
    message: str = Field(..., description="User message to send to DeepSeek-web")
    session_id: Optional[str] = Field(None, description="Chat session ID for context")
    notebook_id: Optional[str] = Field(None, description="Notebook ID for context")
    message_type: Optional[str] = Field(
        "chat",
        description="Message type: chat, summarize, analyze, creative, code",
    )
    headless: Optional[bool] = Field(
        True, description="Run browser headless (True) or headed (False)"
    )


class DeepSeekWebResponse(BaseModel):
    response: Optional[str]
    error: Optional[str]
    manual_login_required: bool
    rate_limited: bool
    session_id: Optional[str]
    prompt_used: Optional[str]


class DeepSeekWebSessionStatus(BaseModel):
    session_active: bool
    cookies_present: bool
    manual_login_required: bool
    last_used_at: Optional[str]


@router.post("/chat/deepseek-web", response_model=DeepSeekWebResponse)
async def deepseek_web_chat(request: DeepSeekWebRequest) -> DeepSeekWebResponse:
    """Send a message to DeepSeek-web via browser-use session capture.

    Transforms the user message through the prompter service (session-aware),
    checks quota limits, runs browser-use to interact with chat.deepseek.com,
    and returns the response.
    """
    try:
        # 1. Quota check
        if not await quota_tracker.can_request("deepseek"):
            raise HTTPException(
                status_code=429,
                detail=(
                    "DeepSeek-web quota exhausted. "
                    "Enable 'Allow local processing' in Settings to use local models, "
                    "or wait for quota reset."
                ),
            )

        # 2. Transform message through prompter
        transformed_prompt = await prompter_service.transform(
            raw_message=request.message,
            session_id=request.session_id,
            notebook_id=request.notebook_id,
            message_type=request.message_type or "chat",
        )

        # 3. Run browser-use
        result = browser_use_service.run_deepseek(
            message=transformed_prompt,
            headless=request.headless if request.headless is not None else True,
            timeout=120.0,
        )

        # 4. Handle manual login required
        if result.get("manual_login_required"):
            raise HTTPException(
                status_code=403,
                detail=(
                    "DeepSeek-web session expired. Manual login required: "
                    "open chat.deepseek.com in a browser, log in, and the session "
                    "cookies will be persisted for future requests."
                ),
            )

        # 5. Handle rate limited
        if result.get("rate_limited"):
            raise HTTPException(
                status_code=429,
                detail=(
                    "DeepSeek-web rate limited. "
                    "Wait for quota reset or enable 'Allow local processing' "
                    "in Settings to use local models."
                ),
            )

        # 6. Increment quota
        await quota_tracker.increment("deepseek", 1)

        return DeepSeekWebResponse(
            response=result.get("response"),
            error=result.get("error"),
            manual_login_required=result.get("manual_login_required", False),
            rate_limited=result.get("rate_limited", False),
            session_id=request.session_id,
            prompt_used=transformed_prompt,
        )

    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        logger.error(f"DeepSeek-web chat error: {e}")
        raise HTTPException(
            status_code=500, detail=f"DeepSeek-web chat failed: {str(e)}"
        )


@router.get(
    "/chat/deepseek-web/session", response_model=DeepSeekWebSessionStatus
)
async def deepseek_web_session_status() -> DeepSeekWebSessionStatus:
    """Return current DeepSeek-web session status."""
    try:
        session_file_exists = SESSION_FILE.exists()
        cookies_present = False
        if session_file_exists:
            import json

            data = json.loads(SESSION_FILE.read_text())
            cookies_present = bool(data.get("cookies"))

        return DeepSeekWebSessionStatus(
            session_active=session_file_exists and cookies_present,
            cookies_present=cookies_present,
            manual_login_required=False,  # Will be True after a failed request
            last_used_at=None,  # Could store last used timestamp
        )
    except Exception as e:  # noqa: BLE001
        logger.error(f"DeepSeek-web session status error: {e}")
        raise HTTPException(
            status_code=500, detail=f"Session status failed: {str(e)}"
        )


@router.post(
    "/chat/deepseek-web/session/refresh", response_model=Dict[str, Any]
)
async def deepseek_web_session_refresh() -> Dict[str, Any]:
    """Refresh DeepSeek-web session cookies (force re-login next request)."""
    try:
        if SESSION_FILE.exists():
            SESSION_FILE.unlink()
        return {"ok": True, "message": "Session cleared. Next request will require fresh login."}
    except Exception as e:  # noqa: BLE001
        logger.error(f"DeepSeek-web session refresh error: {e}")
        raise HTTPException(
            status_code=500, detail=f"Session refresh failed: {str(e)}"
        )


@router.get(
    "/chat/deepseek-web/available", response_model=Dict[str, Any]
)
async def deepseek_web_available() -> Dict[str, Any]:
    """Return whether browser-use is installed and internet is available."""
    import socket

    try:
        socket.create_connection(("1.1.1.1", 53), timeout=3).close()
        internet = True
    except OSError:
        internet = False

    try:
        import browser_use  # noqa: F401

        browser_use_installed = True
    except ImportError:
        browser_use_installed = False

    return {
        "internet_available": internet,
        "browser_use_installed": browser_use_installed,
        "ready": internet and browser_use_installed,
    }
