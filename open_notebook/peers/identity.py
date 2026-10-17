"""This device's own identity, and the token it presents to a peer.

Why a device has an identity at all
-----------------------------------
The cluster is two devices that must ask each other to do work. Until now the
only credential crossing between them was the *sync* token, which is a
cluster-wide secret with no notion of who sent it. That is the wrong shape for
delegation for two reasons:

1. **Revocation is all-or-nothing.** Rotating the sync token to evict one
   device invalidates the other one as well, including a device that was
   never compromised.
2. **It says nothing about the caller.** "Somebody holding the token" is not
   an answer to "which device is asking, and may it".

So each device generates two things once, and keeps them on disk:

- a **device id**, stable across restarts and human-readable enough to
  recognise in a log or a status line;
- a **token**, random per device, which the *peer* stores. This device only
  ever knows its own token; it never has to know how to forge anybody else's.

The peer holding the token is the only party that stores it, so revocation is
deleting one entry from one registry - which is what `registry.py` does.

This file deliberately owns only the local half. Deciding whether a presented
token is acceptable is the peer's job, and that decision lives in the
registry, not here.

What is *not* here
------------------
No shared secret. A cluster key would mean one file per device whose loss
re-opens the whole cluster, and rotation would be a coordinated outage across
devices that cannot see each other. Per-device tokens keep the blast radius of
one leaked file to exactly one device.
"""

from __future__ import annotations

import json
import os
import secrets
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from open_notebook.config import DATA_FOLDER

#: Device credentials live beside the blobs they protect, not in the sync
#: repository. Syncing a credential would mean every device that ever synced
#: holds it, which is the failure this module exists to make impossible.
IDENTITY_FILE = Path(DATA_FOLDER) / "peers" / "identity.json"

#: 32 bytes of entropy, URL-safe. 256 bits is far past any brute-force
#: concern here; the length matters more because it travels in a header.
TOKEN_BYTES = 32

_ID_BYTES = 4  # 8 hex characters; enough to tell two devices apart in a list.


@dataclass(frozen=True)
class DeviceIdentity:
    """This device's id and token.

    `name` is cosmetic - shown in a peer's registry so a human can tell
    `phone` from `pc` without looking up ids. It is never used for an
    authorization decision, because a name is not a secret and is trivially
    chosen by whoever is registering.
    """

    device_id: str
    token: str
    name: str = ""

    def public(self) -> dict[str, Any]:
        """The identity without the token.

        Used for the pairing payload a device hands to its peer. The token is
        transported exactly once, deliberately, and never echoed back by an
        endpoint that merely reports who is registered.
        """
        return {"device_id": self.device_id, "name": self.name}


def generate_device_id() -> str:
    """A short, unique, unguessable-enough device id.

    The `dev-` prefix makes it self-describing in a log line, where a bare
    8-character hex string is easy to mistake for a hash.
    """
    return f"dev-{secrets.token_hex(_ID_BYTES)}"


def generate_token() -> str:
    """A fresh device token."""
    return secrets.token_urlsafe(TOKEN_BYTES)


def _write_private(path: Path, payload: dict[str, Any]) -> None:
    """Write JSON so that only the owner can read it.

    The token is a bearer credential for this device's whole identity, so
    the file is created 0600 and the directory 0700. `os.open` is used rather
    than `Path.write_text` because the mode argument is ignored when the file
    already exists - and an identity file that was created world-readable once
    must not stay that way after a rewrite.

    The write goes to a temporary file and is renamed into place, so a crash
    partway through cannot leave a truncated identity that fails to parse on
    the next boot.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, stat.S_IRWXU)

    tmp = path.with_suffix(".tmp")
    body = json.dumps(payload, indent=2, sort_keys=True)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(body)
            handle.write("\n")
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    os.replace(tmp, path)
    os.chmod(path, 0o600)


def _read(path: Path) -> Optional[dict[str, Any]]:
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return None
    except json.JSONDecodeError as exc:
        # A corrupt identity file is not recoverable by guessing, and
        # silently regenerating would hand the device a *new* id while its
        # peers still hold the old token - every call would then fail as
        # "unknown device", with nothing in the logs to say why.
        raise RuntimeError(
            f"{path} exists but is not valid JSON. It holds this device's "
            f"id and token; deleting it will generate a new identity, which "
            f"peers must be re-paired with. Refusing to overwrite it: {exc}"
        ) from exc


def load_identity(path: Path = IDENTITY_FILE) -> Optional[DeviceIdentity]:
    """This device's identity, or None if it has never been provisioned."""
    data = _read(path)
    if data is None:
        return None
    device_id = data.get("device_id")
    token = data.get("token")
    if not device_id or not token:
        raise RuntimeError(
            f"{path} is missing device_id or token. Refusing to guess which "
            f"one is authoritative; delete the file to re-provision."
        )
    return DeviceIdentity(device_id=device_id, token=token, name=data.get("name", ""))


def ensure_identity(
    path: Path = IDENTITY_FILE, name: str = "", device_id: Optional[str] = None
) -> DeviceIdentity:
    """This device's identity, generating and persisting it on first call.

    Idempotent: calling it on every boot is the intended usage, and it
    returns the same identity every time afterwards. Generating a new one
    would invalidate the token the peer holds, so nothing here may overwrite
    an existing file.
    """
    existing = load_identity(path)
    if existing is not None:
        return existing

    identity = DeviceIdentity(
        device_id=device_id or generate_device_id(),
        token=generate_token(),
        name=name,
    )
    _write_private(
        path,
        {
            "device_id": identity.device_id,
            "token": identity.token,
            "name": identity.name,
        },
    )
    return identity


def set_identity_name(name: str, path: Path = IDENTITY_FILE) -> DeviceIdentity:
    """Give this device a human-readable name, keeping id and token.

    Name only. This cannot rotate a credential, by design - losing the token
    is not something a name change should be able to paper over.
    """
    identity = load_identity(path)
    if identity is None:
        raise RuntimeError(
            "this device has no identity yet; provision it before naming it"
        )
    renamed = DeviceIdentity(identity.device_id, identity.token, name)
    _write_private(
        path,
        {"device_id": renamed.device_id, "token": renamed.token, "name": name},
    )
    return renamed
