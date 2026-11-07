"""Context preprocessor API (B6-B9).

Endpoints:
  POST   /api/preprocess         — preprocess one or more files to `.md`
  GET    /api/preprocess/groups  — file groups and their settings
"""

from __future__ import annotations

from typing import Dict, List, Optional

from fastapi import APIRouter, HTTPException
from loguru import logger
from pydantic import BaseModel, Field

from open_notebook.services.preprocessor_service import (
    FILE_GROUPS,
    preprocessor_service,
)

router = APIRouter()


class PreprocessRequest(BaseModel):
    paths: List[str] = Field(..., description="File paths to preprocess")
    settings: Optional[Dict[str, bool]] = Field(
        None, description="Per-group preprocessing toggles"
    )
    language: Optional[str] = Field(
        None, description="BCP-47 language tag (affects STT tier for audio/video)"
    )


class PreprocessResponse(BaseModel):
    results: List[dict]


class GroupsResponse(BaseModel):
    groups: Dict[str, bool]


@router.post("/preprocess", response_model=PreprocessResponse)
async def preprocess(request: PreprocessRequest):
    """Preprocess one or more files to `.md` alongside the source.

    Rate-limited: one file at a time (B8). Unsupported/misc file types
    are force-enabled (B6).
    """
    try:
        results = await preprocessor_service.preprocess_batch(
            request.paths,
            settings=request.settings or {},
            language=request.language,
        )
        return PreprocessResponse(
            results=[
                {
                    "ok": r.ok,
                    "source_path": r.source_path,
                    "markdown_path": r.markdown_path,
                    "markdown_content": r.markdown_content,
                    "file_group": r.file_group,
                    "force_enabled": r.force_enabled,
                    "error": r.error,
                }
                for r in results
            ]
        )
    except Exception as e:  # noqa: BLE001
        logger.error(f"Preprocess error: {e}")
        raise HTTPException(status_code=500, detail=f"Preprocessing failed: {e}")


@router.get("/preprocess/groups", response_model=GroupsResponse)
async def preprocess_groups():
    """Return file groups and their default preprocessing state.

    Unsupported/misc types are force-enabled (greyed in UI).
    """
    return GroupsResponse(groups={group: True for group in FILE_GROUPS})
