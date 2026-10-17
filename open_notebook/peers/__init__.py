"""Per-device peer identity: this device's credentials, and who it trusts.

Delegation needs a device to be able to ask a peer to run work, and to be able
to tell who is asking. A single shared secret cannot do that - revoking one
device invalidates all of them - so each device holds its own token and each
device stores its peers' tokens.

- `identity.py` - the local half: my id, my token, persisted once.
- `registry.py` - the peer half: who I accept, and where they live.
- `urls.py`    - what an address is allowed to be, since an outbound URL from
  a request body is an SSRF primitive.
- `auth.py`    - the FastAPI dependency that turns a request into an
  authenticated device id.

Nothing here is written to the sync repository. Those files hold bearer
credentials; syncing them would give every device that ever synced the ability
to impersonate every other one.
"""

from open_notebook.peers.identity import (
    DeviceIdentity,
    ensure_identity,
    generate_device_id,
    generate_token,
    load_identity,
    set_identity_name,
)
from open_notebook.peers.registry import (
    PeerEntry,
    PeerRegistry,
    add_peer,
    load_registry,
    local_device_id,
    pairing_payload,
    remove_peer,
    save_registry,
    self_identity,
)
from open_notebook.peers.urls import (
    InvalidPeerURL,
    normalize_base_url,
    validate_peer_url,
)

__all__ = [
    "DeviceIdentity",
    "InvalidPeerURL",
    "PeerEntry",
    "PeerRegistry",
    "add_peer",
    "ensure_identity",
    "generate_device_id",
    "generate_token",
    "load_identity",
    "load_registry",
    "local_device_id",
    "normalize_base_url",
    "pairing_payload",
    "remove_peer",
    "save_registry",
    "self_identity",
    "set_identity_name",
    "validate_peer_url",
]
