"""DeepSeek-web chat endpoints — direct API calls with PC browser-use fallback.

Routes LLM requests through chat.deepseek.com's internal web API using
session cookies (ds_session_id + authorization token). If DeepSeek requires
Proof-of-Work (PoW) that cannot be solved locally, falls back to browser-use
automation on a PC (x86 machine with Chromium).

Endpoints:
  POST /api/chat/deepseek-web — Send a message via direct API (with PoW solving)
  POST /api/chat/deepseek-web/pc-fallback — Send via browser-use on PC
  GET  /api/chat/deepseek-web/session — Session status (credentials configured)
  POST /api/chat/deepseek-web/session/refresh — Clear stored credentials
  POST /api/chat/deepseek-web/session/configure — Store session credentials
  GET  /api/chat/deepseek-web/available — Check if direct API is usable
  GET  /api/chat/deepseek-web/pc-fallback-available — Check if PC browser-use is usable
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException
from loguru import logger
from pydantic import BaseModel, Field

from open_notebook.ai.models import ProcessingFlags
from open_notebook.services.browser_use_service import (
    browser_use_service,
)
from open_notebook.services.deepseek_direct_service import (
    configure as configure_direct,
)
from open_notebook.services.deepseek_direct_service import (
    deepseek_direct_service,
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
    thinking_enabled: Optional[bool] = Field(
        False, description="Enable DeepThink (extended reasoning)"
    )
    pc_fallback: Optional[bool] = Field(
        False, description="Force PC browser-use fallback instead of direct API"
    )


class DeepSeekWebResponse(BaseModel):
    response: Optional[str]
    error: Optional[str]
    manual_login_required: bool
    rate_limited: bool
    pow_required: bool
    pow_solved: bool
    session_id: Optional[str]
    prompt_used: Optional[str]
    used_pc_fallback: bool = False


class DeepSeekWebSessionStatus(BaseModel):
    credentials_configured: bool
    manual_login_required: bool
    rate_limited: bool
    pow_required: bool


class DeepSeekCredentials(BaseModel):
    ds_session_id: str = Field(
        ..., description="ds_session_id cookie from chat.deepseek.com"
    )
    authorization_token: str = Field(
        ...,
        description="Authorization Bearer token from chat.deepseek.com request headers",
    )


class DeepSeekPCFallbackRequest(BaseModel):
    message: str = Field(..., description="User message to send via PC browser-use")
    session_id: Optional[str] = Field(None, description="Chat session ID for context")
    notebook_id: Optional[str] = Field(None, description="Notebook ID for context")
    message_type: Optional[str] = Field(
        "chat",
        description="Message type: chat, summarize, analyze, creative, code",
    )
    thinking_enabled: Optional[bool] = Field(
        False, description="Enable DeepThink (extended reasoning)"
    )


@router.post("/chat/deepseek-web", response_model=DeepSeekWebResponse)
async def deepseek_web_chat(request: DeepSeekWebRequest) -> DeepSeekWebResponse:
    """Send a message to DeepSeek-web via direct API calls.

    Uses session credentials (ds_session_id + authorization_token) stored
    in ProcessingFlags. Attempts local Proof-of-Work solving if required.
    If PoW solving fails and pc_fallback=True, delegates to browser-use
    on a PC (x86 machine with Chromium).

    Requires the user to have configured credentials via
    POST /api/chat/deepseek-web/session/configure.
    """
    try:
        # 1. Load credentials from ProcessingFlags
        flags = await ProcessingFlags.get_instance()
        ds_session_id = flags.deepseek_ds_session_id
        authorization_token = flags.deepseek_authorization_token

        if not ds_session_id or not authorization_token:
            raise HTTPException(
                status_code=403,
                detail=(
                    "DeepSeek-web credentials not configured. "
                    "Log in to chat.deepseek.com in a browser, copy the "
                    "ds_session_id cookie and authorization Bearer token "
                    "from DevTools Network tab, and configure them in Settings."
                ),
            )

        # 2. Configure the direct service with stored credentials
        configure_direct(ds_session_id, authorization_token)

        # 3. Quota check
        if not await quota_tracker.can_request("deepseek"):
            raise HTTPException(
                status_code=429,
                detail=(
                    "DeepSeek-web quota exhausted. "
                    "Enable 'Allow local processing' in Settings to use local models, "
                    "or wait for quota reset."
                ),
            )

        # 4. Transform message through prompter
        transformed_prompt = await prompter_service.transform(
            raw_message=request.message,
            session_id=request.session_id,
            notebook_id=request.notebook_id,
            message_type=request.message_type or "chat",
        )

        # 5. Run direct API call (with PoW solving)
        result = deepseek_direct_service.send_message(
            message=transformed_prompt,
            thinking_enabled=request.thinking_enabled or False,
            timeout=120.0,
        )

        # 6. Handle errors
        if result.get("manual_login_required"):
            raise HTTPException(
                status_code=403,
                detail=(
                    "DeepSeek-web session expired or invalid credentials. "
                    "Re-log in to chat.deepseek.com and update credentials in Settings."
                ),
            )

        if result.get("rate_limited"):
            raise HTTPException(
                status_code=429,
                detail=(
                    "DeepSeek-web rate limited. "
                    "Wait for quota reset or enable 'Allow local processing' "
                    "in Settings to use local models."
                ),
            )

        # 7. If PoW required and not solved, offer PC fallback
        if result.get("pow_required") and not result.get("pow_solved"):
            if request.pc_fallback:
                # Delegate to browser-use on PC
                logger.info("PoW required, delegating to PC browser-use fallback")
                pc_result = await _run_pc_browser_use(
                    message=transformed_prompt,
                    thinking_enabled=request.thinking_enabled or False,
                    session_id=request.session_id,
                    notebook_id=request.notebook_id,
                )
                if pc_result.get("error"):
                    raise HTTPException(
                        status_code=502,
                        detail=f"PC browser-use fallback failed: {pc_result['error']}",
                    )
                await quota_tracker.increment("deepseek", 1)
                return DeepSeekWebResponse(
                    response=pc_result.get("response"),
                    error=pc_result.get("error"),
                    manual_login_required=pc_result.get(
                        "manual_login_required", False
                    ),
                    rate_limited=pc_result.get("rate_limited", False),
                    pow_required=True,
                    pow_solved=False,
                    session_id=request.session_id,
                    prompt_used=transformed_prompt,
                    used_pc_fallback=True,
                )
            else:
                raise HTTPException(
                    status_code=403,
                    detail=(
                        "DeepSeek-web requires Proof-of-Work challenge response. "
                        "Direct API mode does not yet support PoW solving. "
                        "Enable pc_fallback=true to use browser-use on PC, "
                        "or re-log in to chat.deepseek.com and refresh credentials."
                    ),
                )

        if result.get("error"):
            raise HTTPException(
                status_code=500,
                detail=f"DeepSeek-web error: {result['error']}",
            )

        # 8. Increment quota on success
        await quota_tracker.increment("deepseek", 1)

        return DeepSeekWebResponse(
            response=result.get("response"),
            error=result.get("error"),
            manual_login_required=result.get("manual_login_required", False),
            rate_limited=result.get("rate_limited", False),
            pow_required=result.get("pow_required", False),
            pow_solved=result.get("pow_solved", False),
            session_id=request.session_id,
            prompt_used=transformed_prompt,
            used_pc_fallback=False,
        )

    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        logger.error(f"DeepSeek-web chat error: {e}")
        raise HTTPException(
            status_code=500, detail=f"DeepSeek-web chat failed: {str(e)}"
        )


async def _run_pc_browser_use(
    message: str,
    thinking_enabled: bool,
    session_id: Optional[str] = None,
    notebook_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Run browser-use on PC for DeepSeek-web fallback.

    This delegates to the browser_use_service which runs Chromium automation.
    On PC (x86), Chromium is available and browser-use works correctly.
    """
    try:
        result = browser_use_service.run_deepseek(
            message=message,
            headless=True,
            timeout=180.0,
        )
        return {
            "response": result.get("response"),
            "error": result.get("error"),
            "manual_login_required": result.get("manual_login_required", False),
            "rate_limited": result.get("rate_limited", False),
        }
    except Exception as exc:  # noqa: BLE001
        logger.error(f"PC browser-use fallback error: {exc}")
        return {
            "response": None,
            "error": str(exc),
            "manual_login_required": False,
            "rate_limited": False,
        }


@router.post("/chat/deepseek-web/pc-fallback", response_model=DeepSeekWebResponse)
async def deepseek_web_pc_fallback(
    request: DeepSeekPCFallbackRequest,
) -> DeepSeekWebResponse:
    """Send a message to DeepSeek-web via PC browser-use fallback.

    Runs browser-use automation on a PC (x86 machine with Chromium).
    This bypasses Proof-of-Work requirements entirely since a real browser
    handles the challenge automatically.

    Requires the user to have configured credentials via
    POST /api/chat/deepseek-web/session/configure.
    """
    try:
        # 1. Load credentials from ProcessingFlags
        flags = await ProcessingFlags.get_instance()
        ds_session_id = flags.deepseek_ds_session_id
        authorization_token = flags.deepseek_authorization_token

        if not ds_session_id or not authorization_token:
            raise HTTPException(
                status_code=403,
                detail=(
                    "DeepSeek-web credentials not configured. "
                    "Log in to chat.deepseek.com in a browser, copy the "
                    "ds_session_id cookie and authorization Bearer token "
                    "from DevTools Network tab, and configure them in Settings."
                ),
            )

        # 2. Quota check
        if not await quota_tracker.can_request("deepseek"):
            raise HTTPException(
                status_code=429,
                detail=(
                    "DeepSeek-web quota exhausted. "
                    "Enable 'Allow local processing' in Settings to use local models, "
                    "or wait for quota reset."
                ),
            )

        # 3. Transform message through prompter
        transformed_prompt = await prompter_service.transform(
            raw_message=request.message,
            session_id=request.session_id,
            notebook_id=request.notebook_id,
            message_type=request.message_type or "chat",
        )

        # 4. Run PC browser-use
        pc_result = await _run_pc_browser_use(
            message=transformed_prompt,
            thinking_enabled=request.thinking_enabled or False,
            session_id=request.session_id,
            notebook_id=request.notebook_id,
        )

        if pc_result.get("error"):
            raise HTTPException(
                status_code=502,
                detail=f"PC browser-use fallback failed: {pc_result['error']}",
            )

        # 5. Increment quota on success
        await quota_tracker.increment("deepseek", 1)

        return DeepSeekWebResponse(
            response=pc_result.get("response"),
            error=pc_result.get("error"),
            manual_login_required=pc_result.get(
                "manual_login_required", False
            ),
            rate_limited=pc_result.get("rate_limited", False),
            pow_required=False,
            pow_solved=False,
            session_id=request.session_id,
            prompt_used=transformed_prompt,
            used_pc_fallback=True,
        )

    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        logger.error(f"DeepSeek-web PC fallback error: {e}")
        raise HTTPException(
            status_code=500, detail=f"DeepSeek-web PC fallback failed: {str(e)}"
        )


@router.get("/chat/deepseek-web/session", response_model=DeepSeekWebSessionStatus)
async def deepseek_web_session_status() -> DeepSeekWebSessionStatus:
    """Return current DeepSeek-web session/credential status."""
    try:
        flags = await ProcessingFlags.get_instance()
        configured = bool(
            flags.deepseek_ds_session_id and flags.deepseek_authorization_token
        )
        return DeepSeekWebSessionStatus(
            credentials_configured=configured,
            manual_login_required=not configured,
            rate_limited=False,
            pow_required=False,
        )
    except Exception as e:  # noqa: BLE001
        logger.error(f"DeepSeek-web session status error: {e}")
        raise HTTPException(status_code=500, detail=f"Session status failed: {str(e)}")


@router.post("/chat/deepseek-web/session/configure", response_model=Dict[str, Any])
async def deepseek_web_session_configure(
    creds: DeepSeekCredentials,
) -> Dict[str, Any]:
    """Store DeepSeek-web session credentials in ProcessingFlags.

    Obtain these from chat.deepseek.com:
    1. Log in to chat.deepseek.com in a browser
    2. Open DevTools → Network tab
    3. Send any message in the chat
    4. Find the POST request to /api/v0/chat/completion
    5. Copy the Cookie → ds_session_id value
    6. Copy the Headers → authorization Bearer token value
    """
    try:
        flags = await ProcessingFlags.get_instance()
        flags.deepseek_ds_session_id = creds.ds_session_id
        flags.deepseek_authorization_token = creds.authorization_token
        await flags.update()

        # Configure the singleton immediately
        configure_direct(creds.ds_session_id, creds.authorization_token)

        return {
            "ok": True,
            "message": "DeepSeek-web credentials stored successfully.",
        }
    except Exception as e:  # noqa: BLE001
        logger.error(f"DeepSeek-web credential configuration error: {e}")
        raise HTTPException(
            status_code=500, detail=f"Credential configuration failed: {str(e)}"
        )


@router.post("/chat/deepseek-web/session/refresh", response_model=Dict[str, Any])
async def deepseek_web_session_refresh() -> Dict[str, Any]:
    """Clear stored DeepSeek-web credentials (force re-configuration)."""
    try:
        flags = await ProcessingFlags.get_instance()
        flags.deepseek_ds_session_id = None
        flags.deepseek_authorization_token = None
        await flags.update()

        # Reset the singleton
        configure_direct("", "")

        return {"ok": True, "message": "DeepSeek-web credentials cleared."}
    except Exception as e:  # noqa: BLE001
        logger.error(f"DeepSeek-web session refresh error: {e}")
        raise HTTPException(status_code=500, detail=f"Session refresh failed: {str(e)}")


@router.get("/chat/deepseek-web/available", response_model=Dict[str, Any])
async def deepseek_web_available() -> Dict[str, Any]:
    """Return whether DeepSeek-web direct API is configured and internet is available."""
    import socket

    try:
        socket.create_connection(("1.1.1.1", 53), timeout=3).close()
        internet = True
    except OSError:
        internet = False

    try:
        flags = await ProcessingFlags.get_instance()
        configured = bool(
            flags.deepseek_ds_session_id and flags.deepseek_authorization_token
        )
    except Exception:
        configured = False

    return {
        "internet_available": internet,
        "credentials_configured": configured,
        "ready": internet and configured,
    }


@router.get("/chat/deepseek-web/pc-fallback-available", response_model=Dict[str, Any])
async def deepseek_web_pc_fallback_available() -> Dict[str, Any]:
    """Return whether PC browser-use fallback is available.

    Checks if browser-use is installed and internet is available.
    On PC (x86), Chromium is typically available. On phone (ARM),
    browser-use may not work due to lack of Chromium.
    """
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
