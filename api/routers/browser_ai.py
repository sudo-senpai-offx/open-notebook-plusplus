"""Browser AI Mode API (D17).

Endpoints:
  POST   /api/browser-ai/google-ai-mode  — send prompt to Google AI Mode
  POST   /api/browser-ai/duck-ai         — send prompt to Duck.ai
"""

from __future__ import annotations

import os
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException
from loguru import logger
from pydantic import BaseModel, Field

from open_notebook.services.browser_ai_service import browser_ai_service

router = APIRouter()


class GoogleAIModeRequest(BaseModel):
    prompt: str = Field(..., description="Prompt to send to Google AI Mode")
    session_token: Optional[str] = Field(
        None, description="Session token from a previous response for continuation"
    )
    country: str = Field(default="us", description="2-letter ISO country code")
    language: str = Field(default="en", description="2-letter ISO language code")


class GoogleAIModeResponse(BaseModel):
    ok: bool
    response: Optional[str] = None
    session_token: Optional[str] = None
    provider: str = "google-ai-mode"
    error: Optional[str] = None
    rate_limited: bool = False


class DuckAIRequest(BaseModel):
    prompt: str = Field(..., description="Prompt to send to Duck.ai")
    model: Optional[str] = Field(
        None, description="Model name (e.g., gpt-4o-mini, claude-haiku-3.5)"
    )


class DuckAIResponse(BaseModel):
    ok: bool
    response: Optional[str] = None
    provider: str = "duck-ai"
    error: Optional[str] = None
    rate_limited: bool = False


@router.post("/browser-ai/google-ai-mode", response_model=GoogleAIModeResponse)
async def google_ai_mode(request: GoogleAIModeRequest):
    """Send a prompt to Google AI Mode via API wrapper.

    One continuous infinite session per guest/incognito — pass
    `session_token` from a previous response to continue with prior context.
    Semi-automated, rate-limited (max 1 concurrent, 2s between requests).
    """
    try:
        result = await browser_ai_service.google_ai_mode(
            prompt=request.prompt,
            session_token=request.session_token,
            country=request.country,
            language=request.language,
        )
        return GoogleAIModeResponse(
            ok=result.ok,
            response=result.response,
            session_token=result.session_token,
            error=result.error,
            rate_limited=result.rate_limited,
        )
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        logger.error(f"Google AI Mode API error: {e}")
        raise HTTPException(status_code=500, detail=f"Google AI Mode failed: {e}")


@router.post("/browser-ai/duck-ai", response_model=DuckAIResponse)
async def duck_ai(request: DuckAIRequest):
    """Send a prompt to Duck.ai via unofficial bridge.

    Guest multiple sessions — no session series. Rate-limited: ~20 req/60s,
    1s between requests, single active request at a time.
    """
    try:
        result = await browser_ai_service.duck_ai(
            prompt=request.prompt,
            model=request.model,
        )
        return DuckAIResponse(
            ok=result.ok,
            response=result.response,
            error=result.error,
            rate_limited=result.rate_limited,
        )
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        logger.error(f"Duck.ai API error: {e}")
        raise HTTPException(status_code=500, detail=f"Duck.ai failed: {e}")


@router.get("/browser-ai/status", response_model=Dict[str, Any])
async def browser_ai_status():
    """Return browser AI provider availability and configuration status."""
    google_ai_mode_url = os.getenv("GOOGLE_AI_MODE_URL")
    duck_ai_url = os.getenv("DUCK_AI_URL")

    import importlib.util

    p2d_duck_available = importlib.util.find_spec("p2d_duck") is not None
    playwright_available = importlib.util.find_spec("playwright") is not None

    return {
        "google_ai_mode": {
            "configured": bool(google_ai_mode_url),
            "url": google_ai_mode_url,
            "playwright_fallback": playwright_available,
        },
        "duck_ai": {
            "configured": bool(duck_ai_url),
            "url": duck_ai_url,
            "p2d_duck_available": p2d_duck_available,
        },
        "rate_limits": {
            "duck_ai": "~20 req/60s, 1s between, single active request",
            "google_ai_mode": "1 concurrent, 2s between requests",
        },
    }
