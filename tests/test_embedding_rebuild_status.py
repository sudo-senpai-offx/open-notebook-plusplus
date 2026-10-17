"""The rebuild status endpoint, and the enum gap that broke it.

`GET /api/embeddings/rebuild/{id}/status` answered **500** with

    Failed to get rebuild status: 'held' is not a valid CommandStatus

while a rebuild was draining on the phone. `get_command_status` coerces the
stored status into the vendor's `CommandStatus` enum, and `held` - the state
every job is created in - is not a member.

That is the worst shape for this particular bug: on a memory-gated device the
queue is *mostly* held, so the endpoint a person watches to see whether their
rebuild is progressing was broken precisely when they were watching it.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.routers.embedding_rebuild import _PUBLIC_STATUS, _public_status, router


class TestStatusVocabulary:
    def test_held_is_reported_as_queued(self):
        """Held means waiting its turn, which is what `queued` means here."""
        assert _public_status("held") == "queued"

    @pytest.mark.parametrize(
        "stored, expected",
        [
            ("held", "queued"),
            ("new", "queued"),
            ("queued", "queued"),
            ("running", "running"),
            ("completed", "completed"),
            ("failed", "failed"),
        ],
    )
    def test_every_known_state_maps(self, stored: str, expected: str):
        assert _public_status(stored) == expected

    def test_an_unknown_state_is_passed_through(self):
        """Not folded into `completed`.

        Reporting a running rebuild as finished is a lie in the one direction
        that costs the user time - they would stop waiting for work that is
        still going.
        """
        assert _public_status("something-new") == "something-new"

    def test_empty_is_treated_as_queued_not_completed(self):
        assert _public_status("") == "queued"

    def test_the_vendor_enum_gap_is_real(self):
        """Pins *why* this exists.

        If a future vendor release adds `held`, this fails and the fallback
        can be deleted rather than left in place as dead code.
        """
        from surreal_commands import CommandStatus

        members = {m.value for m in CommandStatus}

        if "held" in members:
            pytest.skip("vendor now models `held`; the fallback is dead code")
        assert "held" not in members


@pytest.fixture
def api(monkeypatch):
    app = FastAPI()
    app.include_router(router, prefix="/api/embeddings")
    return TestClient(app)


class TestStatusEndpointSurvivesHeld:
    def test_held_does_not_500(self, api, monkeypatch):
        """The regression: a held job must not become an HTTP 500."""
        import api.routers.embedding_rebuild as mod

        async def explode(command_id):
            raise ValueError("'held' is not a valid CommandStatus")

        async def raw(command_id):
            return "held"

        monkeypatch.setattr(mod, "get_command_status", explode)
        monkeypatch.setattr(mod, "_raw_status", raw)

        response = api.get("/api/embeddings/rebuild/command:abc/status")

        assert response.status_code == 200, response.text
        assert response.json()["status"] == "queued"

    def test_a_missing_command_is_still_404(self, api, monkeypatch):
        """The fallback must not turn every lookup into a success."""
        import api.routers.embedding_rebuild as mod

        async def explode(command_id):
            raise ValueError("'held' is not a valid CommandStatus")

        async def raw(command_id):
            return None

        monkeypatch.setattr(mod, "get_command_status", explode)
        monkeypatch.setattr(mod, "_raw_status", raw)

        response = api.get("/api/embeddings/rebuild/command:abc/status")

        assert response.status_code == 404

    def test_the_happy_path_is_untouched(self, api, monkeypatch):
        """The fallback only runs when the vendor call fails."""
        import api.routers.embedding_rebuild as mod

        async def fine(command_id):
            return type("S", (), {"status": "completed", "result": None})()

        monkeypatch.setattr(mod, "get_command_status", fine)

        response = api.get("/api/embeddings/rebuild/command:abc/status")

        assert response.status_code == 200
        assert response.json()["status"] == "completed"


class TestMappingTable:
    def test_every_value_is_one_the_api_documents(self):
        """The response model promises four states; nothing may leak a fifth."""
        documented = {"queued", "running", "completed", "failed"}

        assert set(_PUBLIC_STATUS.values()) <= documented
