"""Service endpoints: quota status, clipboard, session status.

These endpoints surface the mini-services (quota tracker, clipboard,
browser-use session) to the frontend on existing pages — no new page.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter
from pydantic import BaseModel, Field

from open_notebook.services.clipboard_service import clipboard_service
from open_notebook.services.quota_tracker import quota_tracker

router = APIRouter()


class QuotaProviderUpdate(BaseModel):
    provider: str = Field(..., description="Provider name (e.g. 'deepseek')")
    limit: int = Field(..., ge=0, description="Request limit; 0 = unlimited")
    reset_at: Optional[str] = Field(None, description="ISO timestamp when the limit resets")


class QuotaStatusResponse(BaseModel):
    providers: Dict[str, Any]
    cumulative_used: int
    cumulative_limit: int
    cumulative_remaining: int
    cumulative_exceeded: bool
    updated_at: Optional[str]


class ClipboardReadResponse(BaseModel):
    text: Optional[str]
    image: bool
    available: bool
    error: Optional[str]


@router.get("/services/quota", response_model=QuotaStatusResponse)
async def get_quota_status() -> QuotaStatusResponse:
    """Return current quota status for all cloud providers."""
    status = await quota_tracker.status()
    return QuotaStatusResponse(
        providers=status["providers"],
        cumulative_used=status["cumulative_used"],
        cumulative_limit=status["cumulative_limit"],
        cumulative_remaining=status["cumulative_remaining"],
        cumulative_exceeded=status["cumulative_exceeded"],
        updated_at=status["updated_at"],
    )


@router.put("/services/quota/{provider}")
async def set_quota_limit(provider: str, body: QuotaProviderUpdate) -> Dict[str, Any]:
    """Set the request limit for a cloud provider (0 = unlimited)."""
    await quota_tracker.set_limit(provider, body.limit, body.reset_at)
    return {"ok": True, "provider": provider, "limit": body.limit}


@router.get("/services/clipboard", response_model=ClipboardReadResponse)
async def get_clipboard() -> ClipboardReadResponse:
    """Read the latest clipboard entry from linuxdrop (text + image)."""
    data = clipboard_service.get_latest()
    return ClipboardReadResponse(
        text=data.get("text"),
        image=bool(data.get("image", False)),
        available=bool(data.get("available", False)),
        error=data.get("error"),
    )


@router.get("/services/clipboard/available")
async def clipboard_available() -> Dict[str, Any]:
    """Return whether linuxdrop is installed and reachable."""
    return {
        "available": clipboard_service.is_available(),
        "binary": clipboard_service.binary,
    }
