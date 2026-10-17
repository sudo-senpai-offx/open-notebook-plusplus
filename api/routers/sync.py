"""Sync endpoints. Thin wrapper over `open_notebook.sync.exchange`.

Deliberately no background daemon and no scheduler. Sync is explicit and
operator-initiated because the transport (a git checkout of
`OPEN_NOTEBOOK_SYNC_DIR`) is the operator's, and a job that pushes to
someone else's repository on a timer is not something to enable by
default.
"""

from pathlib import Path

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import FileResponse
from loguru import logger
from pydantic import BaseModel, Field

from open_notebook.domain.notebook import Asset
from open_notebook.exceptions import OpenNotebookError
from open_notebook.peers.urls import InvalidPeerURL, validate_peer_url
from open_notebook.storage.blobs import resolve_blob_path, store_bytes
from open_notebook.sync.blobtransfer import (
    TOKEN_HEADER,
    SyncAuthError,
    check_token,
    local_hashes,
    missing_from_peer,
    missing_hashes,
    offer_to,
    pull_from,
    push_to,
)
from open_notebook.sync.exchange import (
    _read_json,
    _source_kind,
    export_all,
    import_all,
    sync_dir,
)

router = APIRouter()


def checked_peer(peer: str) -> str:
    """Validate an outbound peer address and return its canonical form.

    A URL in a request body that the device then fetches is an SSRF
    primitive: whoever holds the sync token can choose the destination, and
    the connection is made from this device's own network position with its
    own reach. `169.254.169.254` is the address that pays off - on a hosted
    VM it answers with credentials for the whole account, and it is
    reachable from every workload by default.

    Loopback and private ranges stay allowed on purpose: the two devices
    reach each other over an SSH tunnel whose local end *is* loopback, so
    forbidding it would forbid the only topology in use. See
    `open_notebook.peers.urls` for what that leaves open and why it is
    accepted; the pairing registry is the layer that actually pins the
    destination.
    """
    try:
        return validate_peer_url(peer)
    except InvalidPeerURL as exc:
        # 400, not 403: the credential was fine, the request was not. Logging
        # the reason and not the address keeps a probe from filling the log
        # with whatever it tried.
        logger.warning(f"refused outbound peer address: {exc}")
        raise HTTPException(status_code=400, detail=str(exc)) from exc


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


# --------------------------------------------------------------------------
# blob transfer
# --------------------------------------------------------------------------
#
# Separate from the manifest endpoints because they carry a different
# risk: the manifest is hashes and titles, these are the study files
# themselves. Both require the shared token, and both fail closed when
# no token is configured.


class SyncBlobsRequest(BaseModel):
    hashes: list[str] = Field(default_factory=list, description="Blob hashes to fetch")


@router.get("/sync/blobs")
async def sync_blob_list(token: str | None = Header(None, alias=TOKEN_HEADER)):
    """Every blob this device holds, so a peer can ask for what it lacks."""
    try:
        check_token(token)
    except SyncAuthError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    hashes = await local_hashes()
    return {"count": len(hashes), "hashes": hashes}


@router.get("/sync/blobs/{sha256}")
async def sync_blob_get(
    sha256: str, token: str | None = Header(None, alias=TOKEN_HEADER)
):
    """Serve one blob by content hash."""
    try:
        check_token(token)
    except SyncAuthError as exc:
        raise HTTPException(status_code=403, detail=str(exc))

    # Resolve from the hash rather than echoing a caller-supplied path.
    # A hash cannot contain a separator, so this cannot be steered out
    # of the blob root the way a path parameter could.
    resolved = resolve_blob_path(sha256, None)
    if not resolved:
        raise HTTPException(status_code=404, detail="blob not found")
    return FileResponse(resolved, media_type="application/octet-stream")


@router.post("/sync/blobs")
async def sync_blob_offer(
    body: SyncBlobsRequest, token: str | None = Header(None, alias=TOKEN_HEADER)
):
    """Acknowledge which of the offered hashes we can actually fetch.

    This is a notification, not a transfer: the requester already knows
    what it needs from the manifest, so it goes on to GET each hash it
    still lacks.
    """
    try:
        check_token(token)
    except SyncAuthError as exc:
        raise HTTPException(status_code=403, detail=str(exc))

    wanted = await missing_hashes(body.hashes)
    return {"can_serve": len(wanted), "wanted": wanted}


class SyncBlobPullRequest(BaseModel):
    peer: str = Field(..., description="Base URL of the peer to fetch from")
    limit: int = Field(0, description="Cap the transfer; 0 means everything")


@router.post("/sync/blobs/pull")
async def sync_blob_pull(
    body: SyncBlobPullRequest, token: str | None = Header(None, alias=TOKEN_HEADER)
):
    """Fetch every blob this device lacks from *body.peer*.

    Driven through the app rather than the sync script so the transfer
    uses the same store and the same dedup as everything else, instead of
    a second path that could drift.
    """
    try:
        check_token(token)
    except SyncAuthError as exc:
        raise HTTPException(status_code=403, detail=str(exc))

    peer = checked_peer(body.peer)
    wanted = await missing_from_peer(peer, token)
    result = await pull_from(peer, wanted, token=token, limit=body.limit)
    # Bytes that just landed may complete sources imported earlier as
    # metadata: queue their extraction the way an upload would.
    from open_notebook.sync.extraction import enqueue_missing_extraction

    queued = await enqueue_missing_extraction()
    return {"peer": peer, **result, "extraction_queued": queued["queued_count"]}


class SyncBlobOfferRequest(BaseModel):
    peer: str = Field(..., description="Base URL of the peer to offer to")


@router.post("/sync/blobs/offer")
async def sync_blob_offer_local(
    body: SyncBlobOfferRequest, token: str | None = Header(None, alias=TOKEN_HEADER)
):
    """Tell *body.peer* which blobs we hold, so it can fetch what it lacks.

    A notification rather than a transfer: the peer already knows what it
    wants from the manifest, and pushing 49 MB of PDFs unbidden is not a
    courtesy.
    """
    try:
        check_token(token)
    except SyncAuthError as exc:
        raise HTTPException(status_code=403, detail=str(exc))

    ours = await local_hashes()
    peer = checked_peer(body.peer)
    ok = await offer_to(peer, ours, token=token)
    return {"peer": peer, "count": len(ours), "offered": ok}


class SyncBlobPushRequest(BaseModel):
    peer: str = Field(..., description="Base URL of the peer to push to")
    limit: int = Field(0, description="Cap the transfer; 0 means everything")


@router.post("/sync/blobs/push")
async def sync_blob_push(
    body: SyncBlobPushRequest, token: str | None = Header(None, alias=TOKEN_HEADER)
):
    """Push the blobs we hold to a peer that cannot reach us.

    The direction the network requires: the PC can open a connection to
    the phone, the phone cannot open one back.
    """
    try:
        check_token(token)
    except SyncAuthError as exc:
        raise HTTPException(status_code=403, detail=str(exc))

    ours = await local_hashes()
    peer = checked_peer(body.peer)
    result = await push_to(peer, ours, token=token, limit=body.limit)
    return {"peer": peer, "local": len(ours), **result}


@router.post("/sync/blobs/process")
async def sync_blob_process(token: str | None = Header(None, alias=TOKEN_HEADER)):
    """Queue extraction for sources whose files are present but unread.

    A source imported as metadata and completed later by the blob
    channel opens as "No content available" until its text is
    extracted - the bytes resolving is not the same as the text
    existing. This is the hook that connects the two: same
    `process_source` job an upload submits, same queue. Runs on the
    device holding the files, which is the device that received them.
    """
    try:
        check_token(token)
    except SyncAuthError as exc:
        raise HTTPException(status_code=403, detail=str(exc))

    from open_notebook.sync.extraction import enqueue_missing_extraction

    return await enqueue_missing_extraction()


@router.post("/sync/blobs/{sha256}")
async def sync_blob_put(
    sha256: str, request: Request, token: str | None = Header(None, alias=TOKEN_HEADER)
):
    """Accept one blob, keyed by the hash of its own contents.

    The digest is recomputed and compared rather than trusted. Storing
    under a caller-supplied name is how a store ends up where the name and
    the bytes disagree, and nothing downstream would notice - the file
    would simply never hash to what the source claims.
    """
    try:
        check_token(token)
    except SyncAuthError as exc:
        raise HTTPException(status_code=403, detail=str(exc))

    body = await request.body()
    if not body:
        raise HTTPException(status_code=400, detail="empty body")

    ref = store_bytes(body, f"{sha256}.bin")
    if ref.sha256 != sha256.lower():
        # Remove the impostor, but only if this request created it. If
        # the bytes were already present under their true hash then that
        # is a legitimate blob someone else stored, and deleting it to
        # punish a lying peer would be destroying someone else's data.
        if not ref.deduped:
            Path(ref.path).unlink(missing_ok=True)
        raise HTTPException(
            status_code=400,
            detail=(f"content hashes to {ref.sha256}, not {sha256.lower()}"),
        )

    return {"sha256": ref.sha256, "size": ref.size, "deduped": ref.deduped}
