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

from open_notebook.domain.notebook import Asset
from open_notebook.exceptions import OpenNotebookError
from open_notebook.storage.blobs import resolve_blob_path
from open_notebook.sync.exchange import (
    _read_json,
    _source_kind,
    export_all,
    import_all,
    sync_dir,
)

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
    """Where sync reads and writes, and what is waiting there.

    ``sources_awaiting_blobs`` is the number that matters operationally:
    a source whose metadata has synced but whose bytes have not is not
    an error state, it is an outstanding transfer. Reporting it
    separately keeps "sync is broken" distinct from "sync is waiting on
    the blob channel".
    """
    root = sync_dir()
    if not root.is_dir():
        return {
            "sync_dir": str(root),
            "exists": False,
            "notes_present": 0,
            "cards_present": 0,
            "sources_present": 0,
            "sources_awaiting_blobs": 0,
        }

    notes = sorted((root / "notes").glob("*.json"))
    cards = sorted((root / "cards").glob("*.json"))
    sources = sorted((root / "sources").glob("*.json"))

    awaiting = 0
    for path in sources:
        entry = _read_json(path)
        if not entry or not entry.get("sha256"):
            continue
        if _source_kind(Asset(sha256=entry["sha256"], url=entry.get("url"))) != "file":
            continue
        if resolve_blob_path(entry["sha256"], entry.get("filename")) is None:
            awaiting += 1

    return {
        "sync_dir": str(root),
        "exists": True,
        "notes_present": len(notes),
        "cards_present": len(cards),
        "sources_present": len(sources),
        "sources_awaiting_blobs": awaiting,
    }


@router.post("/sync/export")
async def sync_export(body: SyncExportRequest):
    """Write every note, source, card and revlog row into the sync directory."""
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
