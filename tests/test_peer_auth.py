"""Refusing to be used as an SSRF proxy, and recognising a peer device.

`test_metadata_address_is_refused_without_any_outbound_call` is the one that
matters. The device opens the connection itself, from its own network
position, on behalf of whoever holds the sync token - so a request naming
`169.254.169.254` is an attempt to read the host's cloud credentials, and the
check has to happen before a socket is opened rather than after the response
comes back looking uninteresting.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from api.routers.sync import checked_peer, router
from open_notebook.peers.auth import DEVICE_HEADER, authenticate_peer, require_peer
from open_notebook.peers.registry import add_peer, remove_peer
from open_notebook.peers.urls import normalize_base_url

SYNC_TOKEN_ENV = "OPEN_NOTEBOOK_SYNC_TOKEN"
SYNC_TOKEN_HEADER = "X-Sync-Token"

# Destinations that must be refused before anything is dialled.
FORBIDDEN = [
    "http://169.254.169.254/latest/meta-data/iam/security-credentials/",
    "http://[fe80::1]:8080/",
    "file:///data/data/com.termux/files/usr/etc/shadow",
    "gopher://127.0.0.1:70/_evil",
    "http://admin:secret@10.7.109.219:5055",
]


# ------------------------------------------------------------ the guard


class TestCheckedPeer:
    @pytest.mark.parametrize("url", FORBIDDEN)
    def test_dangerous_destinations_are_refused(self, url: str):
        with pytest.raises(HTTPException) as caught:
            checked_peer(url)

        assert caught.value.status_code == 400, (
            "a bad address is a bad request, not a bad credential"
        )

    def test_a_real_peer_passes_and_comes_back_canonical(self):
        assert checked_peer("http://127.0.0.1:15055/") == "http://127.0.0.1:15055"

    def test_the_tunnel_addresses_this_deployment_uses_pass(self):
        """Loopback is allowed on purpose. Forbidding it would forbid the only
        topology in use, and the rule would be disabled within a week."""
        for url in (
            "http://127.0.0.1:15055",
            "http://127.0.0.1:18000",
            "http://10.7.109.219:5055",
            "http://10.158.2.78:5055",
        ):
            assert checked_peer(url) == url


# ------------------------------------------------------- at the endpoint


@pytest.fixture
def api(tmp_path: Path, monkeypatch):
    monkeypatch.setenv(SYNC_TOKEN_ENV, "unit-test-token")
    monkeypatch.setattr(
        "open_notebook.storage.blobs._root", lambda override=None: tmp_path / "blobs"
    )

    app = FastAPI()
    app.include_router(router, prefix="/api")
    return TestClient(app)


@pytest.fixture
def dialled(monkeypatch):
    """Record any outbound peer call, so a refusal can be shown to be early.

    Asserting only on the 400 would pass even if the request were dialled and
    the answer discarded afterwards.
    """
    calls: list[str] = []

    async def record(url: str, *args, **kwargs):
        calls.append(url)
        return {}

    for name in ("missing_from_peer", "pull_from", "push_to", "offer_to"):
        monkeypatch.setattr(f"api.routers.sync.{name}", record)

    # `enqueue_missing_extraction` is imported *inside* the pull handler, so
    # patching the router namespace would not reach it - and the handler
    # would then try to reach SurrealDB, answering 403 for a reason that has
    # nothing to do with the address under test.
    async def no_extraction():
        return {"queued_count": 0}

    monkeypatch.setattr(
        "open_notebook.sync.extraction.enqueue_missing_extraction", no_extraction
    )

    return calls


class TestEndpointsRefuseBeforeDialling:
    @pytest.mark.parametrize("verb", ["pull", "push", "offer"])
    def test_metadata_address_is_refused_without_any_outbound_call(
        self, api, dialled, verb
    ):
        headers = {SYNC_TOKEN_HEADER: "unit-test-token"}

        response = api.post(
            f"/api/sync/blobs/{verb}",
            json={"peer": "http://169.254.169.254/latest/meta-data/"},
            headers=headers,
        )

        assert response.status_code == 400
        assert dialled == [], f"{verb} dialled {dialled} despite refusing the address"

    @pytest.mark.parametrize("url", FORBIDDEN)
    def test_every_forbidden_shape_is_refused_end_to_end(self, api, dialled, url):
        headers = {SYNC_TOKEN_HEADER: "unit-test-token"}

        response = api.post("/api/sync/blobs/pull", json={"peer": url}, headers=headers)

        assert response.status_code == 400, f"{url} was not refused"
        assert dialled == []

    def test_a_registered_peer_reaches_the_transfer(self, api, dialled):
        """The guard must not have become a blanket refusal.

        Without this, a test asserting only on rejections would pass just as
        happily against an endpoint that refused everything.
        """
        headers = {SYNC_TOKEN_HEADER: "unit-test-token"}

        response = api.post(
            "/api/sync/blobs/pull",
            json={"peer": "http://127.0.0.1:15055"},
            headers=headers,
        )

        assert response.status_code == 200
        assert dialled, "a legitimate peer was refused"

    def test_a_bad_address_does_not_become_an_unauthenticated_grace(self, api, dialled):
        """The address is checked after the token, so a bad credential still
        reads as 403 - never as 400, which would tell an unauthenticated
        caller that its request was otherwise well-formed."""
        response = api.post(
            "/api/sync/blobs/pull",
            json={"peer": "http://169.254.169.254/"},
            headers={SYNC_TOKEN_HEADER: "wrong-token"},
        )

        assert response.status_code == 403
        assert dialled == []


# ---------------------------------------------------------- peer auth


@pytest.fixture
def registry(tmp_path: Path) -> Path:
    path = tmp_path / "registry.json"
    add_peer("dev-pc", "tok-pc", ["http://127.0.0.1:15056"], path=path)
    return path


class TestAuthenticatePeer:
    def test_a_paired_device_passes(self, registry: Path):
        from open_notebook.peers.registry import load_registry

        peer = authenticate_peer(load_registry(registry), "dev-pc", "tok-pc")

        assert peer.device_id == "dev-pc"

    def test_missing_headers_are_refused(self, registry: Path):
        from open_notebook.peers.registry import load_registry

        loaded = load_registry(registry)
        with pytest.raises(HTTPException) as caught:
            authenticate_peer(loaded, None, None)
        assert caught.value.status_code == 401

    def test_missing_device_header_names_the_header(self, registry: Path):
        """A caller should not have to read the source to find out what to send."""
        from open_notebook.peers.registry import load_registry

        with pytest.raises(HTTPException, match=DEVICE_HEADER):
            authenticate_peer(load_registry(registry), None, "tok-pc")

    def test_wrong_token_is_401(self, registry: Path):
        from open_notebook.peers.registry import load_registry

        with pytest.raises(HTTPException) as caught:
            authenticate_peer(load_registry(registry), "dev-pc", "tok-nope")
        assert caught.value.status_code == 401

    def test_unknown_device_is_401_not_403(self, registry: Path):
        """Never paired is a guess, not an incident."""
        from open_notebook.peers.registry import load_registry

        with pytest.raises(HTTPException) as caught:
            authenticate_peer(load_registry(registry), "dev-stranger", "tok")
        assert caught.value.status_code == 401

    def test_a_revoked_device_is_403(self, registry: Path):
        """Deliberate eviction is an incident, and worth distinguishing from a
        stale client in the logs."""
        from open_notebook.peers.registry import load_registry

        remove_peer("dev-pc", path=registry)

        with pytest.raises(HTTPException) as caught:
            authenticate_peer(load_registry(registry), "dev-pc", "tok-pc")
        assert caught.value.status_code == 403
        assert "revoked" in caught.value.detail.lower()

    def test_verification_reads_the_registry_each_call(
        self, registry: Path, monkeypatch
    ):
        """Revocation happens without a restart. A cached registry would keep
        honouring a token the operator revoked ten minutes ago."""
        import asyncio

        import open_notebook.peers.auth as authmod

        monkeypatch.setattr(authmod, "REGISTRY_FILE", registry)

        assert (
            asyncio.run(require_peer("dev-pc", "Bearer tok-pc")).device_id == "dev-pc"
        )

        remove_peer("dev-pc", path=registry)

        with pytest.raises(HTTPException) as caught:
            asyncio.run(require_peer("dev-pc", "Bearer tok-pc"))
        assert caught.value.status_code == 403


class TestBearerParsing:
    @pytest.mark.parametrize(
        "header, expected",
        [
            ("Bearer tok", "tok"),
            ("bearer tok", "tok"),
            ("BEARER tok", "tok"),
            ("Bearer  tok  ", "tok"),
            ("Basic tok", None),
            ("tok", None),
            ("Bearer", None),
            ("Bearer   ", None),
            ("", None),
            (None, None),
        ],
    )
    def test_only_a_well_formed_bearer_header_yields_a_credential(
        self, header, expected
    ):
        from open_notebook.peers.auth import _bearer

        assert _bearer(header) == expected

    def test_a_credential_is_never_read_from_a_url(self):
        """A token in a URL ends up in the registry file, in logs, and in any
        error quoting it."""
        from open_notebook.peers.urls import InvalidPeerURL

        with pytest.raises(InvalidPeerURL, match="credentials"):
            normalize_base_url("http://tok@127.0.0.1:15055")

        # And the endpoint surfaces it as a bad request, not as a 500.
        with pytest.raises(HTTPException) as caught:
            checked_peer("http://tok@127.0.0.1:15055")
        assert caught.value.status_code == 400
