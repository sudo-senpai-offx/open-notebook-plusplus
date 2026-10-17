"""Which devices this one accepts work from, and where it will send its own.

The peer side of `identity.py`. A device stores a registry of the devices
paired with it: their id, their token, and the base URLs they are reachable
at. Three operations matter, and they are deliberately separate:

- **pair** (`add_peer`) - record a device's token and addresses;
- **verify** (`verify_peer`) - decide whether a presented credential is one
  we issued, without leaking *which* part was wrong;
- **revoke** (`remove_peer`) - forget a device, so its token stops working.

Revocation deletes one entry. That is the entire point of per-device tokens
and the reason this module exists: evicting a lost phone is one removal here,
and the PC's own token is untouched.

Why URLs are stored here at all
-------------------------------
Because an outbound URL arriving in a request body is an SSRF primitive, and
a denylist is the wrong tool for it - a denylist has to enumerate every
address that should not be reachable, and misses one. Registering the exact
addresses a paired device may be reached at inverts the problem: the safe set
is enumerated, and anything unrecognised is refused. It also doubles as the
routing table, so "which peer do I delegate this to" has a single answer.

Not in the sync repository
--------------------------
The registry holds bearer tokens. Syncing it would hand every device that has
ever synced the ability to impersonate every other one.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from open_notebook.config import DATA_FOLDER
from open_notebook.peers.identity import (
    IDENTITY_FILE,
    DeviceIdentity,
    _read,
    _write_private,
    ensure_identity,
    generate_token,
    load_identity,
)
from open_notebook.peers.urls import InvalidPeerURL, normalize_base_url

#: The peer's trust list. Beside the identity file so the two are backed up
#: and restored together; neither is meaningful without the other.
REGISTRY_FILE = Path(DATA_FOLDER) / "peers" / "registry.json"

_DEFAULT_PORTS = {"http": 80, "https": 443}


@dataclass
class PeerEntry:
    """One paired device.

    `token` is the credential that device presents. It is stored in the clear
    because this is a device-local file with 0600 permissions in a data
    directory that already holds the blob store in the clear; encrypting it
    would add a key whose loss silently breaks every peer relationship. The
    threat being defended against is a token crossing to another device over
    sync, not an attacker with root on the device that issued it.
    """

    device_id: str
    token: str
    base_urls: list[str] = field(default_factory=list)
    name: str = ""

    def public(self) -> dict[str, Any]:
        """The entry without its token, for any endpoint that lists peers."""
        return {
            "device_id": self.device_id,
            "name": self.name,
            "base_urls": list(self.base_urls),
        }

    def serves(self, url: str) -> bool:
        """Whether *url* is one of this peer's registered addresses."""
        try:
            candidate = normalize_base_url(url)
        except InvalidPeerURL:
            return False
        for base in self.base_urls:
            try:
                normalized = normalize_base_url(base)
            except InvalidPeerURL:
                # A stored base URL that no longer parses is unusable, and
                # silently ignoring it would quietly shrink what this device
                # will talk to. Skip it: the pairing is re-done when it is
                # needed, and the failure is visible then.
                continue
            if normalized == candidate:
                return True
        return False


@dataclass
class PeerRegistry:
    """The set of devices this one trusts."""

    peers: dict[str, PeerEntry] = field(default_factory=dict)
    #: Devices that were paired and then deliberately evicted. Kept so that a
    #: rejection can distinguish "never heard of it" from "we removed it on
    #: purpose", which is the difference between a misconfiguration and an
    #: incident worth investigating.
    revoked: list[str] = field(default_factory=list)

    # -- lookup ---------------------------------------------------------

    def get(self, device_id: str) -> Optional[PeerEntry]:
        return self.peers.get(device_id)

    def verify_peer(self, device_id: Optional[str], token: Optional[str]) -> bool:
        """Whether this device issued that token to *device_id*.

        A missing id or token is a failed verification, not an error: the
        caller's credentials are wrong either way, and the two cases must not
        be distinguishable from outside.

        The comparison is constant-time. A short-circuiting `!=` on the token
        would return faster for a wrong first byte, which is enough to
        recover a token one byte at a time given a few thousand attempts -
        and "a few thousand local requests" is not a large budget.
        """
        if not device_id or not token:
            return False
        entry = self.peers.get(device_id)
        if entry is None:
            # Still burn a comparison so that an unknown device is not
            # distinguishable from a known one by response time.
            secrets.compare_digest(b"", b"")
            return False
        return secrets.compare_digest(
            entry.token.encode("utf-8"), token.encode("utf-8")
        )

    def peer_for_url(self, url: str) -> Optional[PeerEntry]:
        """The paired peer reachable at *url*, or None if none is registered.

        None is the answer for an unregistered address, which is what makes
        refusing to fetch from it a default rather than a special case.
        """
        for entry in self.peers.values():
            if entry.serves(url):
                return entry
        return None

    def is_revoked(self, device_id: str) -> bool:
        return device_id in self.revoked

    def listing(self) -> list[dict[str, Any]]:
        """Every peer, without tokens."""
        return [
            entry.public()
            for entry in sorted(self.peers.values(), key=lambda e: e.device_id)
        ]


def load_registry(path: Path = REGISTRY_FILE) -> PeerRegistry:
    """The registry on disk, or an empty one if this device has no peers yet."""
    data = _read(path)
    if data is None:
        return PeerRegistry()
    peers: dict[str, PeerEntry] = {}
    for device_id, raw in (data.get("peers") or {}).items():
        peers[device_id] = PeerEntry(
            device_id=device_id,
            token=raw.get("token", ""),
            base_urls=list(raw.get("base_urls") or []),
            name=raw.get("name", ""),
        )
    return PeerRegistry(peers=peers, revoked=list(data.get("revoked") or []))


def save_registry(registry: PeerRegistry, path: Path = REGISTRY_FILE) -> None:
    """Persist the registry, owner-readable only."""
    _write_private(
        path,
        {
            "peers": {
                device_id: {
                    "token": entry.token,
                    "base_urls": entry.base_urls,
                    "name": entry.name,
                }
                for device_id, entry in registry.peers.items()
            },
            "revoked": list(registry.revoked),
        },
    )


def add_peer(
    device_id: str,
    token: Optional[str] = None,
    base_urls: Optional[list[str]] = None,
    name: str = "",
    path: Path = REGISTRY_FILE,
) -> PeerEntry:
    """Pair a device, or update one that is already paired.

    Re-pairing with a new token rotates that device's credential, which is
    the recovery path for a device whose file was lost. Addresses are
    replaced wholesale rather than merged: a device that moved has a stale
    address in the old list, and leaving it would keep accepting traffic for
    wherever it used to be.

    `token` may be omitted to generate one here, which is how the *other*
    device gets its credential: this device issues it and hands it over
    during pairing.
    """
    if not device_id or not device_id.strip():
        raise ValueError("device_id is required")
    if token is not None and not token.strip():
        raise ValueError(
            "token was supplied but is empty. An empty token would pair a "
            "device whose credential is the empty string, which anything "
            "could present; omit the argument to have one generated."
        )

    registry = load_registry(path)
    normalized_urls: list[str] = []
    for url in base_urls or []:
        try:
            normalized_urls.append(normalize_base_url(url))
        except InvalidPeerURL as exc:
            raise ValueError(
                f"cannot register {url!r} as a peer address: {exc}"
            ) from exc

    entry = PeerEntry(
        device_id=device_id.strip(),
        token=token or generate_token(),
        base_urls=normalized_urls,
        name=name,
    )
    registry.peers[entry.device_id] = entry
    if entry.device_id in registry.revoked:
        registry.revoked.remove(entry.device_id)
    save_registry(registry, path)
    return entry


def remove_peer(device_id: str, path: Path = REGISTRY_FILE) -> bool:
    """Revoke a device: its token stops working immediately.

    Returns whether there was anything to remove, so a caller revoking twice
    can tell the difference between a successful second pass and a typo in the
    device id - the difference between an idempotent script and a broken one.
    """
    registry = load_registry(path)
    removed = registry.peers.pop(device_id, None) is not None
    if device_id not in registry.revoked:
        registry.revoked.append(device_id)
    save_registry(registry, path)
    return removed


def pairing_payload(
    identity: Optional[DeviceIdentity] = None,
    identity_path: Path = IDENTITY_FILE,
) -> dict[str, Any]:
    """What this device hands to a peer to be paired with it.

    The only place a device's own token ever leaves it, and it leaves exactly
    once, deliberately, during pairing.
    """
    mine = identity or ensure_identity(identity_path)
    return {
        "device_id": mine.device_id,
        "token": mine.token,
        "name": mine.name,
    }


def self_identity(path: Path = IDENTITY_FILE) -> DeviceIdentity:
    """This device's identity, provisioning one if needed.

    A thin alias for `ensure_identity` so callers reading about *peers* do
    not have to know that the local half lives in `identity.py`.
    """
    return ensure_identity(path)


def local_device_id(path: Path = IDENTITY_FILE) -> Optional[str]:
    """This device's id, or None if not provisioned.

    Never provisions: something that reports who we are should not be the
    thing that decides we exist.
    """
    identity = load_identity(path)
    return identity.device_id if identity else None


__all__ = [
    "PeerEntry",
    "PeerRegistry",
    "REGISTRY_FILE",
    "add_peer",
    "load_registry",
    "local_device_id",
    "pairing_payload",
    "remove_peer",
    "save_registry",
    "self_identity",
]
