"""What an outbound peer address is allowed to be, and why each rule exists.

This module exists because a request body carrying a URL that the server then
fetches is an SSRF primitive. Given `POST /api/sync/blobs/pull {"peer": "..."}`
the device will open that connection from its own network position, with its
own reach, on behalf of whoever holds the sync token.

Two layers, and the difference between them matters
---------------------------------------------------
**This file is the denylist.** It refuses the address classes that are almost
never a legitimate peer and are the ones that pay off: the cloud metadata
endpoint, multicast, unspecified. It cannot be complete - a hostname that
resolves to a blocked address passes, because resolving it here would mean
resolving it twice, and the second resolution (the one that actually connects)
is the one an attacker controls. DNS rebinding defeats any denylist that does
not pin the address.

**`registry.py` is the allowlist**, and it is the real fix: a peer address has
to be one we paired. Enumerating the safe set cannot be defeated by a clever
address, because the address is the thing being checked.

So this layer narrows the blast radius for an unregistered caller, and the
registry decides who is registered at all.

Loopback and private ranges are deliberately allowed
----------------------------------------------------
The blocking instinct is to reject `127.0.0.1` and `10.x` as well. That would
break this deployment completely: the two devices reach each other over an
SSH tunnel whose local end *is* loopback, and neither can dial the other
directly. A rule that forbids the only topology in use is a rule that will be
disabled within a week, at which point it protects nothing.

The exposure that leaves is real and is accepted rather than hidden: a paired
or token-holding caller can make the device connect to services on its own
loopback interface and LAN. Loopback services here are this app's own API and
SurrealDB, and the caller already holds a token that grants full API access -
so the marginal gain from the loopback pivot is a database port, not a
privilege boundary. The metadata endpoint is the different case, and it is
blocked.
"""

from __future__ import annotations

import ipaddress
from typing import Optional
from urllib.parse import urlsplit, urlunsplit

_ALLOWED_SCHEMES = frozenset({"http", "https"})


class InvalidPeerURL(ValueError):
    """An address that must not be used as a peer."""


def _bracketed_to_bare(host: str) -> str:
    """`[::1]` -> `::1`. `urlsplit` keeps IPv6 hosts bracketed."""
    if host.startswith("[") and host.endswith("]"):
        return host[1:-1]
    return host


def _forbidden_reason(host: str) -> Optional[str]:
    """Why *host* must never be connected to, or None if it is merely risky.

    Only applies to literal addresses. A hostname is not inspected, and that
    is the gap described in the module docstring.
    """
    bare = _bracketed_to_bare(host)
    try:
        addr = ipaddress.ip_address(bare)
    except ValueError:
        return None

    if addr.is_loopback:
        # Checked before `is_reserved` because Python classifies ::1 as
        # reserved, and IPv6 loopback is as legitimate here as IPv4 loopback.
        # See the module docstring: the tunnel ends on loopback, so forbidding
        # it would forbid the only topology in use.
        return None
    if addr.is_link_local:
        # 169.254.169.254 is the cloud instance metadata endpoint. On a
        # hosted VM, one request here can hand back credentials for the whole
        # account, and it is reachable from every workload by default. This is
        # the single highest-value address in the blocklist.
        return "link-local addresses expose host metadata and are not peers"
    if addr.is_multicast:
        return "multicast addresses are not peers"
    if addr.is_unspecified:
        return "the unspecified address is not a peer"
    if addr.is_reserved:
        return "reserved addresses are not peers"
    return None


def normalize_base_url(url: str) -> str:
    """Canonical form of a peer base URL, or raise `InvalidPeerURL`.

    Canonicalisation is what makes address matching reliable: a registered
    `http://Phone.local:5055` and a request for `http://phone.local:5055/`
    are the same peer, and a comparison that missed that would silently
    refuse a legitimate device.

    Rejected, and each for a reason:
      - no scheme or a non-HTTP one - `file://` and `gopher://` turn a fetch
        into an arbitrary-protocol client;
      - credentials embedded in the URL, which would put a password in
        logs, in the registry file, and in any error message quoting it;
      - no host, which is a relative path, not an address;
      - the blocked address classes above.
    """
    if not url or not url.strip():
        raise InvalidPeerURL("address is empty")

    raw = url.strip()
    try:
        parts = urlsplit(raw)
    except ValueError as exc:
        raise InvalidPeerURL(f"not a usable address: {exc}") from exc

    scheme = parts.scheme.lower()
    if not scheme:
        raise InvalidPeerURL(
            "address has no scheme; write it as http://host or https://host"
        )
    if scheme not in _ALLOWED_SCHEMES:
        raise InvalidPeerURL(
            f"scheme {scheme!r} is not allowed; only http and https are"
        )

    if parts.username or parts.password:
        raise InvalidPeerURL(
            "address carries credentials; pass a token in a header instead, "
            "so it does not end up in the registry, in logs, or in errors"
        )

    host = (parts.hostname or "").lower()
    if not host:
        raise InvalidPeerURL("address has no host")

    reason = _forbidden_reason(host)
    if reason:
        raise InvalidPeerURL(f"{host}: {reason}")

    # Drop the port when it is the scheme's default, so that
    # `http://pc:80` and `http://pc` compare equal.
    port = parts.port  # raises ValueError on a malformed port
    netloc = host if port is None else f"{host}:{port}"
    if port is not None and _DEFAULT_PORT(scheme) == port:
        netloc = host
    # IPv6 literals must stay bracketed in a netloc.
    if ":" in host:
        netloc = f"[{host}]"
        if port is not None and _DEFAULT_PORT(scheme) != port:
            netloc = f"[{host}]:{port}"

    path = parts.path.rstrip("/")
    return urlunsplit((scheme, netloc, path, "", ""))


def _DEFAULT_PORT(scheme: str) -> Optional[int]:  # noqa: N802
    return {"http": 80, "https": 443}.get(scheme)


def validate_peer_url(url: str) -> str:
    """Normalise *url*, refusing anything a peer must not be.

    The single entry point for request-supplied addresses. Returns the
    canonical form on success so callers can compare or store it without
    re-parsing.
    """
    return normalize_base_url(url)
