"""Blob transfer between two devices.

The endpoints serve the raw bytes of a study library, so the tests here
are mostly about the boundary: no token means no service, a wrong token
means no service, and a hash that is not a 64-char digest cannot be
turned into a path. Those are the failure modes that would matter if
this ran on a shared network.

The happy path is verified between two real blob stores, since the point
of the whole exercise is that bytes land in the peer correctly.
"""

from pathlib import Path

import pytest

from open_notebook.storage.blobs import store_bytes
from open_notebook.sync.blobtransfer import (
    SYNC_TOKEN_ENV,
    SyncAuthError,
    check_token,
    local_hashes,
    missing_hashes,
)


@pytest.fixture
def store(tmp_path: Path, monkeypatch):
    """Point the blob store at a temp dir."""
    import open_notebook.storage.blobs as blobmod

    root = tmp_path / "blobs"
    monkeypatch.setattr(
        blobmod,
        "_root",
        lambda override=None: root if override is None else Path(override),
    )
    return root


class TestTokenIsRequired:
    """Fail closed. An unconfigured device must not serve anything."""

    def test_no_token_configured_refuses(self, monkeypatch):
        monkeypatch.delenv(SYNC_TOKEN_ENV, raising=False)
        with pytest.raises(SyncAuthError):
            check_token("anything")

    def test_empty_token_is_not_configured(self, monkeypatch):
        monkeypatch.setenv(SYNC_TOKEN_ENV, "   ")
        with pytest.raises(SyncAuthError):
            check_token("anything")

    def test_correct_token_passes(self, monkeypatch):
        monkeypatch.setenv(SYNC_TOKEN_ENV, "s3cret")
        check_token("s3cret")

    def test_wrong_token_refuses(self, monkeypatch):
        monkeypatch.setenv(SYNC_TOKEN_ENV, "s3cret")
        with pytest.raises(SyncAuthError):
            check_token("wrong")

    def test_missing_token_refuses(self, monkeypatch):
        monkeypatch.setenv(SYNC_TOKEN_ENV, "s3cret")
        with pytest.raises(SyncAuthError):
            check_token(None)

    def test_empty_presented_token_refuses(self, monkeypatch):
        monkeypatch.setenv(SYNC_TOKEN_ENV, "s3cret")
        with pytest.raises(SyncAuthError):
            check_token("")

    def test_prefix_of_the_token_is_refused(self, monkeypatch):
        # Guards against a prefix comparison slipping through.
        monkeypatch.setenv(SYNC_TOKEN_ENV, "s3cret-longer")
        with pytest.raises(SyncAuthError):
            check_token("s3cret")


class TestLocalInventory:
    @pytest.mark.asyncio
    async def test_empty_store_lists_nothing(self, store):
        assert await local_hashes() == []

    @pytest.mark.asyncio
    async def test_lists_stored_hashes(self, store):
        a = store_bytes(b"alpha", "a.pdf", root=store)
        b = store_bytes(b"beta", "b.pdf", root=store)
        assert await local_hashes() == sorted([a.sha256, b.sha256])

    @pytest.mark.asyncio
    async def test_suffix_is_stripped_from_the_hash(self, store):
        a = store_bytes(b"alpha", "a.pdf", root=store)
        # The stored name is <hash>.pdf; the hash reported must be bare.
        assert await local_hashes() == [a.sha256]
        assert all("." not in h for h in await local_hashes())

    @pytest.mark.asyncio
    async def test_duplicates_do_not_appear_twice(self, store):
        store_bytes(b"same", "one.pdf", root=store)
        store_bytes(b"same", "two.pdf", root=store)
        assert len(await local_hashes()) == 1

    @pytest.mark.asyncio
    async def test_missing_hashes_filters_what_we_have(self, store):
        a = store_bytes(b"have", "h.pdf", root=store)
        absent = "0" * 64
        assert await missing_hashes([a.sha256, absent]) == [absent]


class TestTransfer:
    @pytest.mark.asyncio
    async def test_bytes_land_in_the_peer_store(self, store, tmp_path, monkeypatch):
        """A blob written by one store is readable by another via hash."""
        payload = b"a lecture, transferred between devices"
        origin = store_bytes(payload, "lecture.pdf", root=store)

        # Stand in for the peer's store.
        import open_notebook.storage.blobs as blobmod

        peer_root = tmp_path / "peer-blobs"
        monkeypatch.setattr(blobmod, "_root", lambda override=None: peer_root)

        # Serve it over a real HTTP server so the transport is exercised.
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer

        from open_notebook.sync.blobtransfer import fetch_one

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == f"/api/sync/blobs/{origin.sha256}":
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                else:
                    self.send_response(404)
                    self.end_headers()

            def log_message(self, *args):
                pass

        server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            got = await fetch_one(
                f"http://127.0.0.1:{server.server_port}", origin.sha256
            )
        finally:
            server.shutdown()

        assert got == origin.sha256
        from open_notebook.storage.blobs import resolve_blob_path

        landed = resolve_blob_path(origin.sha256)
        assert landed is not None
        assert Path(landed).read_bytes() == payload

    @pytest.mark.asyncio
    async def test_a_peer_serving_the_wrong_bytes_is_rejected(
        self, store, tmp_path, monkeypatch
    ):
        """A peer that lies about a hash must not poison the store.

        Storing under the claimed name would make `source.local_file`
        resolve to content that is not the source, and nothing downstream
        would notice - the name and the bytes would simply disagree.
        """
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer

        import open_notebook.storage.blobs as blobmod

        peer_root = tmp_path / "peer-blobs"
        monkeypatch.setattr(blobmod, "_root", lambda override=None: peer_root)

        claimed = "a" * 64
        impostor = b"completely different bytes"

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Length", str(len(impostor)))
                self.end_headers()
                self.wfile.write(impostor)

            def log_message(self, *args):
                pass

        server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            from open_notebook.sync.blobtransfer import fetch_one

            got = await fetch_one(f"http://127.0.0.1:{server.server_port}", claimed)
        finally:
            server.shutdown()

        assert got is None, "content hashing to the wrong digest must be refused"
        from open_notebook.storage.blobs import resolve_blob_path

        assert resolve_blob_path(claimed) is None


class TestPushDirection:
    """The direction this network actually needs.

    The phone cannot open a connection to the PC, so it can never pull.
    The PC has to push, and a channel with only `pull_from` is a channel
    that does not work here.
    """

    @pytest.mark.asyncio
    async def test_push_delivers_bytes_to_the_peer(self, store, tmp_path, monkeypatch):
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer

        payload = b"pushed across the network"
        ref = store_bytes(payload, "push-me.pdf", root=store)

        received: dict[str, bytes] = {}

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length)
                received[self.path] = body
                self.send_response(201)
                self.end_headers()

            def log_message(self, *args):
                pass

        server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            from open_notebook.sync.blobtransfer import push_to

            result = await push_to(
                f"http://127.0.0.1:{server.server_port}", [ref.sha256]
            )
        finally:
            server.shutdown()

        assert result["sent"] == [ref.sha256]
        assert result["failed"] == []
        body = received[f"/api/sync/blobs/{ref.sha256}"]
        assert body == payload, "the peer must receive the bytes, not a reference"

    @pytest.mark.asyncio
    async def test_push_skips_blobs_we_do_not_hold(self, store):
        from open_notebook.sync.blobtransfer import push_to

        out = await push_to("http://127.0.0.1:1", ["0" * 64])
        assert out["offered"] == 1
        assert out["sent"] == []
        assert out["failed"] == [], "a blob we do not hold is skipped, not a failure"

    @pytest.mark.asyncio
    async def test_push_reports_a_refusal(self, store, tmp_path):
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer

        ref = store_bytes(b"refused bytes", "r.bin", root=store)

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                self.send_response(403)
                self.end_headers()

            def log_message(self, *args):
                pass

        server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            from open_notebook.sync.blobtransfer import push_to

            out = await push_to(f"http://127.0.0.1:{server.server_port}", [ref.sha256])
        finally:
            server.shutdown()

        assert out["sent"] == []
        assert out["failed"] == [ref.sha256], (
            "a refusal must be reported, not swallowed"
        )


class TestPutVerifiesTheDigest:
    """The upload endpoint must recompute the hash, not trust the name.

    Accepting the caller's name would leave the store claiming a file is
    one thing while it is another, and `Source.local_file` would resolve
    to the wrong content with nothing to notice.
    """

    @pytest.fixture
    def client(self, store, tmp_path, monkeypatch):
        monkeypatch.setenv(SYNC_TOKEN_ENV, "unit-test-token")
        import open_notebook.storage.blobs as blobmod

        peer = tmp_path / "peer"
        peer.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(blobmod, "_root", lambda override=None: peer)

        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from api.routers.sync import router

        app = FastAPI()
        app.include_router(router, prefix="/api")
        return TestClient(app), peer

    def test_a_peer_cannot_store_mislabeled_bytes(self, client):
        import hashlib

        from open_notebook.storage.blobs import resolve_blob_path

        api, peer = client
        headers = {"X-Sync-Token": "unit-test-token"}
        payload = b"honest bytes"
        good = hashlib.sha256(payload).hexdigest()

        assert (
            api.post(
                f"/api/sync/blobs/{good}", content=payload, headers=headers
            ).status_code
            == 200
        )
        assert resolve_blob_path(good) is not None

        lie = "a" * 64
        response = api.post(f"/api/sync/blobs/{lie}", content=payload, headers=headers)
        assert response.status_code == 400
        assert resolve_blob_path(lie) is None, (
            "the impostor must not be filed under the claimed hash"
        )

    def test_a_rejected_upload_never_deletes_a_blob_it_did_not_create(self, client):
        """Regression: punishing a liar must not destroy someone else's data.

        The rejected bytes may hash to something already in the store -
        legitimately stored by an earlier upload. Deleting that to clean
        up after the liar would delete a valid file. Only remove the blob
        if this request is what put it there.
        """
        import hashlib

        from open_notebook.storage.blobs import resolve_blob_path

        api, peer = client
        headers = {"X-Sync-Token": "unit-test-token"}
        payload = b"legitimately stored bytes"
        good = hashlib.sha256(payload).hexdigest()

        api.post(f"/api/sync/blobs/{good}", content=payload, headers=headers)
        assert resolve_blob_path(good) is not None

        # Same bytes, mislabelled. The blob already exists, so this must
        # be rejected *without* touching it.
        response = api.post(
            f"/api/sync/blobs/{'c' * 64}", content=payload, headers=headers
        )
        assert response.status_code == 400
        assert resolve_blob_path(good) is not None, (
            "a rejected upload deleted a blob it did not create"
        )

        # A fresh impostor, which this request *did* create, is cleaned up.
        api.post(
            f"/api/sync/blobs/{'d' * 64}", content=b"new impostor", headers=headers
        )
        left = [p for p in peer.rglob("*") if p.is_file()]
        assert len(left) == 1, f"impostor not cleaned up: {left}"

    def test_upload_requires_a_token(self, client):
        import hashlib

        api, _ = client
        good = hashlib.sha256(b"x").hexdigest()
        response = api.post(f"/api/sync/blobs/{good}", content=b"x")
        assert response.status_code == 403

    def test_empty_body_is_rejected(self, client):
        api, _ = client
        response = api.post(
            "/api/sync/blobs/" + "0" * 64,
            content=b"",
            headers={"X-Sync-Token": "unit-test-token"},
        )
        assert response.status_code == 400


@pytest.fixture
def roots(store, monkeypatch):
    """Point the storage roots at the temp store.

    Without this the LFI guard rejects every path as being outside the
    configured roots, and the test would be measuring the guard rather
    than the resolution.
    """
    import api.routers.sources as routes

    monkeypatch.setattr(routes, "BLOBS_FOLDER", str(store))
    monkeypatch.setattr(routes, "UPLOADS_FOLDER", str(store / "uploads"))
    return store


class TestTheAppResolvesSyncedSources:
    """A synced source has a hash and no path. The app must cope.

    This is the gap that made the whole feature look broken: the blob
    channel delivered 51 MB, the store held every byte, and the API still
    reported `file_available: None` for all 46 sources because every
    consumer read `asset.file_path` instead of the derived
    `Source.local_file`. Storage worked; nothing above it did.
    """

    @pytest.fixture
    def synced(self, store, roots):
        """A source exactly as import leaves it: hash, no path."""
        import open_notebook.domain.notebook as nb

        ref = store_bytes(b"a lecture, delivered by the blob channel", "Lecture.pdf")
        return ref, nb.Source(
            title="Lecture",
            asset=nb.Asset(sha256=ref.sha256, filename="Lecture.pdf", size=ref.size),
        )

    def test_no_stored_path_yet_the_file_resolves(self, synced):
        ref, source = synced
        assert source.asset.file_path is None, "precondition: import stores no path"
        assert source.local_file == ref.path
        assert source.has_local_file is True

    def test_availability_is_not_none(self, synced):
        """The value the API hands the UI."""
        from api.routers.sources import _is_source_file_available

        _ref, source = synced
        assert _is_source_file_available(source) is True

    def test_availability_is_false_when_the_blob_is_missing(self, store, roots):
        import open_notebook.domain.notebook as nb

        absent = "d" * 64
        source = nb.Source(
            title="Not fetched yet",
            asset=nb.Asset(sha256=absent, filename="Later.pdf", size=10),
        )
        from api.routers.sources import _is_source_file_available

        assert _is_source_file_available(source) is False

    def test_link_source_still_reports_none(self):
        """A link has no file and no hash; that is not an error."""
        import open_notebook.domain.notebook as nb
        from api.routers.sources import _is_source_file_available

        source = nb.Source(title="A link", asset=nb.Asset(url="https://example.com/x"))
        assert _is_source_file_available(source) is None

    def test_the_api_response_carries_the_identity(self, synced):
        """The UI needs the filename - blobs are stored under their hash."""
        from api.models import AssetModel

        ref, source = synced
        model = AssetModel(
            file_path=source.local_file,
            url=source.asset.url,
            sha256=source.asset.sha256,
            filename=source.asset.filename,
            size=source.asset.size,
        )
        assert model.sha256 == ref.sha256
        assert model.filename == "Lecture.pdf"
        assert model.file_path == ref.path


class TestListEndpointSerialisation:
    """The list route builds its response straight from the query row.

    It never goes through `_source_to_response`, so anything omitted
    there simply does not reach the UI. That is how a synced source came
    back with `sha256: null` and no `file_available` while its bytes sat
    in the store - the detail route was fixed and the list route was
    not, so the fix looked complete and was not.
    """

    def test_row_becomes_an_asset_with_its_identity(self, store, roots):
        from api.routers.sources import _asset_from_row

        ref = store_bytes(b"row content", "Row.pdf", root=store)
        asset = _asset_from_row(
            {"sha256": ref.sha256, "filename": "Row.pdf", "size": ref.size}
        )
        assert asset is not None
        assert asset.sha256 == ref.sha256
        assert asset.filename == "Row.pdf"
        assert asset.file_path == ref.path

    def test_row_availability_matches_the_instance_helper(self, store, roots):
        import open_notebook.domain.notebook as nb
        from api.routers.sources import _available_from_row, _is_source_file_available

        ref = store_bytes(b"availability content", "Avail.pdf", root=store)
        row = {"sha256": ref.sha256, "filename": "Avail.pdf", "size": ref.size}
        source = nb.Source(title="x", asset=nb.Asset(**row))

        assert _available_from_row(row) is True
        assert _is_source_file_available(source) is True

    def test_pending_transfer_reads_false_from_both_paths(self, store, roots):
        from api.routers.sources import _available_from_row

        row = {"sha256": "e" * 64, "filename": "Later.pdf", "size": 1}
        assert _available_from_row(row) is False

    def test_link_and_text_rows_read_none(self, store, roots):
        from api.routers.sources import _available_from_row

        assert _available_from_row(None) is None
        assert _available_from_row({"url": "https://example.com"}) is None
        assert _available_from_row({"filename": None, "sha256": None}) is None

    def test_a_stored_path_still_works_for_legacy_rows(self, store, roots):
        """Pre-blob rows have no hash; their path is all we have."""
        from api.routers.sources import _asset_from_row

        legacy = {"file_path": "/somewhere/old.pdf", "url": None}
        asset = _asset_from_row(legacy)
        assert asset is not None
        assert asset.file_path == "/somewhere/old.pdf"
        assert asset.sha256 is None
