from fastapi import APIRouter, HTTPException, Request
from loguru import logger

router = APIRouter()


def _governor_of(request: Request):
    governor = getattr(request.app.state, "governor", None)
    if governor is None:
        raise HTTPException(
            status_code=503, detail="governor not running in this process"
        )
    return governor


@router.post("/governor/wake")
async def wake_worker(request: Request):
    """Restart a hibernated worker and clear the draining flag.

    Deliberately authenticated (not in the middleware exempt list): waking
    the worker resumes compute spend.
    """
    governor = _governor_of(request)
    try:
        await governor.wake()
    except Exception as e:
        logger.warning(f"governor wake failed: {e}")
        raise HTTPException(status_code=502, detail=f"wake failed: {e}") from e
    return {"woke": True}


@router.get("/governor/state")
async def governor_state(request: Request):
    """Whether the worker is currently hibernated by the governor."""
    governor = _governor_of(request)
    return {"asleep": bool(getattr(governor, "_asleep", False))}
