"""Transfer blob bytes between devices.

The manifest carries hashes; this moves the files they name. They are
deliberately separate channels because a hash is safe to put in a git
repository and a PDF is not - and because separating them lets either
side fail independently. A device can perfectly well import metadata for
a source whose bytes have not arrived, and that state has to be visible
rather than mistaken for an error.

Direction is not symmetric on this network: the phone cannot open a TCP
connection to the PC (measured - ICMP succeeds, no SYN arrives), so
over a LAN the PC pulls from the phone. `pull_from` and `push_to` exist
for both anyway, because USB and cloud do not share that constraint.

Every request carries a shared token. This endpoint serves the raw bytes
of a study library to anyone who can reach it, which on a shared or
campus network is not an acceptable default - so a device with no token
configured refuses the transfer rather than serving openly.
"""

from __future__ import annotations

import os
import secrets
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence

import httpx
from loguru import logger

from open_notebook.storage.blobs import (
    blob_exists,
    resolve_blob_path,
    store_bytes,
)

SYNC_TOKEN_ENV = "OPEN_NOTEBOOK_SYNC_TOKEN"
TOKEN_HEADER = "X-Sync-Token"
DEFAULT_TIMEOUT = 300.0


class SyncAuthError(RuntimeError):
    """Raised when a peer is not authorised to exchange blobs."""


def configured_token() -> Optional[str]:
    token = os.getenv(SYNC_TOKEN_ENV, "").strip()
    return token or None


def check_token(presented: Optional[str]) -> None:
    """Verify a presented token, refusing when none is configured.

    Failing closed matters: a device that has not set
    ``OPEN_NOTEBOOK_SYNC_TOKEN`` must not serve its library to the local
    network just because nobody configured anything.
    """
    expected = configured_token()
    if expected is None:
        raise SyncAuthError(
            f"{SYNC_TOKEN_ENV} is not set, so blob transfer is disabled. "
            "Set it to the same value on both devices to enable it."
        )
    if not presented or not secrets.compare_digest(presented, expected):
        raise SyncAuthError("invalid or missing sync token")


def _headers(token: Optional[str]) -> Dict[str, str]:
    if not token:
        return {}
    return {TOKEN_HEADER: token}


async def local_hashes() -> List[str]:
    """Every blob this device holds.

    Walks the store rather than the database so it reflects bytes that
    are actually present, including blobs whose source record was
    deleted. Offering one of those costs nothing - the peer hashes it,
    sees it already has it, and moves on.
    """
    from open_notebook.storage.blobs import _root

    found: List[str] = []
    root = _root()
    if not root.is_dir():
        return found
    for shard in root.iterdir():
        if not shard.is_dir():
            continue
        for entry in shard.iterdir():
            stem = entry.name.split(".", 1)[0]
            if len(stem) == 64:
                found.append(stem)
    return sorted(found)


async def missing_hashes(wanted: Sequence[str]) -> List[str]:
    """Filter *wanted* down to the hashes this device does not hold."""
    return [h for h in wanted if not blob_exists(h)]


async def fetch_one(
    base_url: str, sha256: str, token: Optional[str] = None
) -> Optional[str]:
    """Download one blob. Returns its hash, or None if unavailable."""
    url = f"{base_url.rstrip('/')}/api/sync/blobs/{sha256}"
    try:
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
            response = await client.get(url, headers=_headers(token))
    except httpx.HTTPError as exc:
        logger.warning(f"blob fetch failed for {sha256[:12]}: {exc}")
        return None

    if response.status_code != 200:
        logger.debug(f"peer has no blob {sha256[:12]} (HTTP {response.status_code})")
        return None

    # Store by content, not by the hash the peer claimed. If the peer sent
    # something else, store_bytes puts it under *its* hash and the
    # mismatch shows up as the blob never becoming available - rather
    # than the name silently disagreeing with the bytes.
    ref = store_bytes(response.content, f"{sha256}.bin")
    if ref.sha256 != sha256:
        logger.error(
            f"peer sent content hashing to {ref.sha256[:12]}, not {sha256[:12]}; discarded"
        )
        ref_dup = resolve_blob_path(ref.sha256)
        if ref_dup:
            Path(ref_dup).unlink(missing_ok=True)
        return None
    return ref.sha256


async def pull_from(
    base_url: str,
    wanted: Iterable[str],
    token: Optional[str] = None,
    limit: int = 0,
) -> Dict[str, object]:
    """Fetch every missing blob from a peer.

    *limit* caps the transfer in one run, so pulling a large library does
    not block a sync indefinitely - the remainder is picked up next time.
    """
    target = list(wanted)
    if limit and len(target) > limit:
        target = target[:limit]

    fetched: List[str] = []
    absent: List[str] = []
    for sha256 in target:
        got = await fetch_one(base_url, sha256, token)
        (fetched if got else absent).append(sha256)

    return {
        "requested": len(target),
        "fetched": fetched,
        "absent": absent,
        "failed": len(absent),
    }


async def offer_to(
    base_url: str, hashes: Sequence[str], token: Optional[str] = None
) -> bool:
    """Ask a peer to fetch the blobs we hold.

    One request listing every hash, rather than N uploads: the peer
    already knows what it needs from the manifest, so this is a
    notification, not a transfer.
    """
    url = f"{base_url.rstrip('/')}/api/sync/blobs"
    payload = {"hashes": list(hashes)}
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(url, json=payload, headers=_headers(token))
    except httpx.HTTPError as exc:
        logger.warning(f"blob offer failed: {exc}")
        return False
    return response.status_code in (200, 204)


async def peer_hashes(base_url: str, token: Optional[str] = None) -> List[str]:
    """Ask a peer what it holds."""
    url = f"{base_url.rstrip('/')}/api/sync/blobs"
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.get(url, headers=_headers(token))
    except httpx.HTTPError as exc:
        logger.warning(f"could not list peer blobs at {base_url}: {exc}")
        return []
    if response.status_code != 200:
        logger.warning(f"peer refused the blob listing (HTTP {response.status_code})")
        return []
    try:
        return list(response.json().get("hashes") or [])
    except ValueError:
        return []


async def missing_from_peer(base_url: str, token: Optional[str] = None) -> List[str]:
    """Hashes the peer holds that this device does not."""
    ours = set(await local_hashes())
    return [h for h in await peer_hashes(base_url, token) if h not in ours]


async def push_to(
    base_url: str,
    hashes: Sequence[str],
    token: Optional[str] = None,
    limit: int = 0,
) -> Dict[str, object]:
    """Upload blobs we hold to a peer.

    This is the direction the network actually needs. The phone cannot
    open a TCP connection to the PC - measured, ICMP succeeds and no SYN
    arrives - so `pull_from` is useless to it in that direction and the
    PC has to push instead. Offering first (`offer_to`) and pushing only
    what the peer asked for keeps a 49 MB library from being uploaded to
    a device that already has most of it.

    One blob per request rather than a multipart batch: it keeps memory
    flat for a large PDF and lets a single failure not sink the rest.
    """
    targets = list(hashes)
    if limit and len(targets) > limit:
        targets = targets[:limit]

    sent: List[str] = []
    failed: List[str] = []
    for sha256 in targets:
        local = resolve_blob_path(sha256)
        if not local:
            continue
        try:
            data = Path(local).read_bytes()
        except OSError as exc:
            logger.warning(f"cannot read {local}: {exc}")
            failed.append(sha256)
            continue

        url = f"{base_url.rstrip('/')}/api/sync/blobs/{sha256}"
        try:
            async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
                response = await client.post(
                    url,
                    content=data,
                    headers={
                        **_headers(token),
                        "Content-Type": "application/octet-stream",
                    },
                )
        except httpx.HTTPError as exc:
            logger.warning(f"blob push failed for {sha256[:12]}: {exc}")
            failed.append(sha256)
            continue

        if response.status_code in (200, 201, 204):
            sent.append(sha256)
        else:
            logger.warning(f"peer refused {sha256[:12]} (HTTP {response.status_code})")
            failed.append(sha256)

    return {"offered": len(targets), "sent": sent, "failed": failed}
