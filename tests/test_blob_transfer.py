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
