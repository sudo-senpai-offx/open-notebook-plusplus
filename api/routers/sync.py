"""Sync endpoints. Thin wrapper over `open_notebook.sync.exchange`.

Deliberately no background daemon and no scheduler. Sync is explicit and
operator-initiated because the transport (a git checkout of
`OPEN_NOTEBOOK_SYNC_DIR`) is the operator's, and a job that pushes to
someone else's repository on a timer is not something to enable by
default.
"""

from fastapi import APIRouter, HTTPException
from loguru import logger
from pydantic import BaseModel, Field

from open_notebook.exceptions import OpenNotebookError
from open_notebook.sync.exchange import export_all, import_all, sync_dir

router = APIRouter()


class SyncExportRequest(BaseModel):
    device: str = Field(
        ...,
        description="Name of this install, recorded in the manifest so a "
        "conflicting file says who last wrote it",
        max_length=64,
    )


class SyncImportRequest(BaseModel):
    dry_run: bool = Field(
        default=True,
        description="Report what would change without writing. Default on, "
        "because a sync that overwrites is not something to discover by "
        "running it",
    )


@router.get("/sync/status")
async def sync_status():
    """Where sync reads and writes, and whether that directory exists."""
    root = sync_dir()
    return {
        "sync_dir": str(root),
        "exists": root.is_dir(),
        "notes_present": len(list((root / "notes").glob("*.json")))
        if root.is_dir()
        else 0,
        "cards_present": len(list((root / "cards").glob("*.json")))
        if root.is_dir()
        else 0,
    }


@router.post("/sync/export")
async def sync_export(body: SyncExportRequest):
    """Write every note, card and revlog row into the sync directory."""
    try:
        manifest = await export_all(body.device)
        return {"ok": True, **manifest, "sync_dir": str(sync_dir())}
    except OpenNotebookError:
        raise
    except Exception as exc:
        logger.warning(f"sync export failed: {exc}")
        raise HTTPException(status_code=500, detail=f"Export failed: {exc}")


@router.post("/sync/import")
async def sync_import(body: SyncImportRequest):
    """Upsert everything in the sync directory by stable id."""
    try:
        result = await import_all(dry_run=body.dry_run)
        return {"ok": True, "sync_dir": str(sync_dir()), **result}
    except OpenNotebookError:
        raise
    except Exception as exc:
        logger.warning(f"sync import failed: {exc}")
        raise HTTPException(status_code=500, detail=f"Import failed: {exc}")
