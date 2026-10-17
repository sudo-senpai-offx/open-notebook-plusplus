"""Turning a request into an authenticated device id.

Layered on top of the existing password middleware rather than replacing it,
because the two answer different questions:

- **the password** answers *may this caller use this API at all* - operator
  authority, and the only thing standing between the phone and its LAN;
- **a device token** answers *which device is this, and may it ask for that* -
  machine identity, and what delegation actually needs.

So a delegation request carries both. Dropping the password requirement would
mean any device holding a token could reach endpoints the operator never
opened to it, and dropping the token requirement would mean the delegation
endpoints could only answer "yes, someone with the password", which is the
ambiguity that made delegation unsafe to build in the first place.

The dependency never returns a token or reads one out of a URL. It accepts
only the header form, so a credential cannot end up in a log line, a registry
file, or an exception message.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from fastapi import Header, HTTPException, status

from open_notebook.peers.registry import (
    REGISTRY_FILE,
    PeerRegistry,
    load_registry,
)

#: Which device is calling. Distinct from the password: it is the peer's claim
#: about who it is, checked against the registry.
DEVICE_HEADER = "X-OpenNotebook-Device"

#: Set on every peer-authenticated response so an operator can tell which
#: requests were machine-to-machine without reading logs.
PEER_IDENTITY_HEADER = "X-OpenNotebook-Peer"


@dataclass(frozen=True)
class AuthenticatedPeer:
    """A device that presented a token this device issued."""

    device_id: str
    name: str = ""


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


def _bearer(header: Optional[str]) -> Optional[str]:
    """The credential from an `Authorization: Bearer x` header.

    Returns None for anything else - a missing header, a different scheme, or
    a header with no credential. All three are the same failure from outside.
    """
    if not header:
        return None
    try:
        scheme, credentials = header.split(" ", 1)
    except ValueError:
        return None
    if scheme.lower() != "bearer":
        return None
    credentials = credentials.strip()
    return credentials or None


def authenticate_peer(
    registry: PeerRegistry,
    device_id: Optional[str],
    token: Optional[str],
) -> AuthenticatedPeer:
    """Resolve a request's credentials to a paired device, or refuse.

    Two distinct refusals, because they mean different things to whoever is
    reading the log later:

    - **401** the credential is absent, malformed, or not one we issued -
      a bug, a stale client, or a guess;
    - **403** the device is one we paired and then deliberately evicted. That
      is an incident, not a mistake, and it is the case worth an alert.

    The 403 also reveals that the id was once known. That is intentional: it
    is only reachable by a caller who already knows a real, previously-issued
    device id, and suppressing it would make a real eviction indistinguishable
    from a typo in the operator's own configuration.
    """
    if not device_id:
        raise _unauthorized(f"missing {DEVICE_HEADER} header")
    if not token:
        raise _unauthorized("missing or malformed Authorization: Bearer header")

    if registry.verify_peer(device_id, token):
        entry = registry.get(device_id)
        assert entry is not None  # verify_peer already consulted this entry
        return AuthenticatedPeer(device_id=device_id, name=entry.name)

    if registry.is_revoked(device_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                f"device {device_id!r} was revoked on this device. If that was "
                f"deliberate, re-pair it; if not, treat it as a compromise."
            ),
        )

    raise _unauthorized("unknown device, or its token does not match")


async def require_peer(
    device_id: Optional[str] = Header(None, alias=DEVICE_HEADER),
    authorization: Optional[str] = Header(None),
) -> AuthenticatedPeer:
    """Dependency: this request comes from a paired device.

    Reads the registry on each call rather than caching it, because pairing
    and revocation happen without a restart, and a cached copy would keep
    honouring a token the operator revoked ten minutes ago. The file is small
    and this is not on a hot path.
    """
    return authenticate_peer(
        load_registry(REGISTRY_FILE), device_id, _bearer(authorization)
    )


__all__ = [
    "AuthenticatedPeer",
    "DEVICE_HEADER",
    "PEER_IDENTITY_HEADER",
    "authenticate_peer",
    "require_peer",
]
