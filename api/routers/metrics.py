from fastapi import APIRouter, HTTPException
from loguru import logger

from open_notebook.exceptions import OpenNotebookError
from open_notebook.metrics.summary import build_summary

router = APIRouter()


@router.get("/metrics/summary")
async def metrics_summary():
    """Study metrics derived live from revlog + focus sessions. No counters."""
    try:
        return await build_summary()
    except OpenNotebookError:
        raise
    except Exception as e:
        logger.warning(f"metrics summary failed: {e}")
        raise HTTPException(status_code=500, detail="Failed to build summary")
