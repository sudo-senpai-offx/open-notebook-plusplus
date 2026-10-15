"""Characterization tests for the chat and source-chat routers.

These pin down the behaviors shared (via copy-paste) between
`api/routers/chat.py` and `api/routers/source_chat.py` before/after extracting
them into `api/routers/_chat_shared.py`:

- record-ID normalization (bare id vs already-prefixed id)
- session/source verification (missing record -> 404, missing `refers_to`
  relation -> 404 on every method now that the routers re-raise HTTPException
  instead of swallowing it into a 500)
- LangGraph state -> `ChatMessage` extraction shapes (type/content fallbacks)

DB access and LangGraph state are mocked following the style of
tests/test_crud_404.py.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from open_notebook.exceptions import NotFoundError


@pytest.fixture
def client():
    from api.main import app

    return TestClient(app)


def _nf(*_args, **_kwargs):
    raise NotFoundError("not found")


def _session(**overrides):
    """A ChatSession-like object with the attributes the routers read."""
    defaults = dict(
        id="chat_session:abc",
        title="My Session",
        created="2026-01-01T00:00:00",
        updated="2026-01-02T00:00:00",
        model_override=None,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _source(**overrides):
    defaults = dict(id="source:xyz", title="My Source")
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _graph_state(values):
    state = MagicMock()
    state.values = values
    return state


class _Msg:
    """A LangChain-like message with id/type/content."""

    def __init__(self, id, type, content):
        self.id = id
        self.type = type
        self.content = content


class _Bare:
    """An object with no type/content attributes (exercises the fallbacks)."""

    def __str__(self):
        return "bare-repr"


# --- chat.py: ID normalization ------------------------------------------------


@pytest.mark.asyncio
@patch("api.routers.chat.repo_query", new_callable=AsyncMock)
@patch("api.routers.chat.chat_graph")
@patch("api.routers.chat.ChatSession.get", new_callable=AsyncMock)
async def test_get_chat_session_bare_id_gets_prefixed(
    mock_get, mock_graph, mock_repo, client
):
    mock_get.return_value = _session()
    mock_graph.get_state.return_value = _graph_state({"messages": []})
    mock_repo.return_value = [{"out": "notebook:1"}]

    resp = client.get("/api/chat/sessions/abc")

    assert resp.status_code == 200
    mock_get.assert_awaited_once_with("chat_session:abc")


@pytest.mark.asyncio
@patch("api.routers.chat.repo_query", new_callable=AsyncMock)
@patch("api.routers.chat.chat_graph")
@patch("api.routers.chat.ChatSession.get", new_callable=AsyncMock)
async def test_get_chat_session_prefixed_id_kept_as_is(
    mock_get, mock_graph, mock_repo, client
):
    mock_get.return_value = _session()
    mock_graph.get_state.return_value = _graph_state({"messages": []})
    mock_repo.return_value = [{"out": "notebook:1"}]

    resp = client.get("/api/chat/sessions/chat_session:abc")

    assert resp.status_code == 200
    mock_get.assert_awaited_once_with("chat_session:abc")


@pytest.mark.asyncio
@patch("api.routers.chat.ChatSession.get", new_callable=AsyncMock)
async def test_delete_chat_session_missing_returns_404(mock_get, client):
    mock_get.side_effect = _nf
    resp = client.delete("/api/chat/sessions/gone")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Session not found"
    mock_get.assert_awaited_once_with("chat_session:gone")


# --- chat.py: message extraction shape ----------------------------------------


@pytest.mark.asyncio
@patch("api.routers.chat.repo_query", new_callable=AsyncMock)
@patch("api.routers.chat.chat_graph")
@patch("api.routers.chat.ChatSession.get", new_callable=AsyncMock)
async def test_get_chat_session_message_shapes(mock_get, mock_graph, mock_repo, client):
    mock_get.return_value = _session()
    mock_graph.get_state.return_value = _graph_state(
        {"messages": [_Msg("m1", "human", "hello"), _Msg("m2", "ai", "hi"), _Bare()]}
    )
    mock_repo.return_value = [{"out": "notebook:1"}]

    resp = client.get("/api/chat/sessions/abc")

    assert resp.status_code == 200
    body = resp.json()
    assert body["message_count"] == 3
    assert body["messages"][0] == {
        "id": "m1",
        "type": "human",
        "content": "hello",
        "timestamp": None,
    }
    assert body["messages"][1]["type"] == "ai"
    # Object without type/content falls back to "unknown" / str(msg); the id
    # fallback is positional (msg_<index>).
    assert body["messages"][2] == {
        "id": "msg_2",
        "type": "unknown",
        "content": "bare-repr",
        "timestamp": None,
    }


@pytest.mark.asyncio
@patch("api.routers.chat.repo_query", new_callable=AsyncMock)
@patch("api.routers.chat.chat_graph")
@patch("api.routers.chat.ChatSession.get", new_callable=AsyncMock)
async def test_get_chat_session_no_state_yields_empty_messages(
    mock_get, mock_graph, mock_repo, client
):
    mock_get.return_value = _session()
    mock_graph.get_state.return_value = None
    mock_repo.return_value = []

    resp = client.get("/api/chat/sessions/abc")

    assert resp.status_code == 200
    body = resp.json()
    assert body["messages"] == []
    assert body["message_count"] == 0
    assert body["notebook_id"] is None


# --- source_chat.py: source verification --------------------------------------


@pytest.mark.asyncio
@patch("api.routers._chat_shared.Source.get", new_callable=AsyncMock)
async def test_create_source_chat_session_missing_source_returns_404(mock_get, client):
    mock_get.side_effect = _nf
    resp = client.post("/api/sources/gone/chat/sessions", json={"source_id": "gone"})
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Source not found"
    mock_get.assert_awaited_once_with("source:gone")


@pytest.mark.asyncio
@patch("api.routers._chat_shared.Source.get", new_callable=AsyncMock)
async def test_list_source_chat_sessions_missing_source_returns_404(mock_get, client):
    mock_get.side_effect = _nf
    resp = client.get("/api/sources/source:gone/chat/sessions")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Source not found"
    mock_get.assert_awaited_once_with("source:gone")


# --- source_chat.py: session + relation verification ---------------------------


@pytest.mark.asyncio
@patch("api.routers._chat_shared.repo_query", new_callable=AsyncMock)
@patch("api.routers._chat_shared.ChatSession.get", new_callable=AsyncMock)
@patch("api.routers._chat_shared.Source.get", new_callable=AsyncMock)
async def test_get_source_chat_session_missing_session_returns_404(
    mock_source_get, mock_session_get, mock_repo, client
):
    mock_source_get.return_value = _source()
    mock_session_get.side_effect = _nf

    resp = client.get("/api/sources/xyz/chat/sessions/gone")

    assert resp.status_code == 404
    assert resp.json()["detail"] == "Source or session not found"
    mock_source_get.assert_awaited_once_with("source:xyz")
    mock_session_get.assert_awaited_once_with("chat_session:gone")


@pytest.mark.asyncio
@patch("api.routers._chat_shared.repo_query", new_callable=AsyncMock)
@patch("api.routers._chat_shared.ChatSession.get", new_callable=AsyncMock)
@patch("api.routers._chat_shared.Source.get", new_callable=AsyncMock)
async def test_get_source_chat_session_missing_relation_behavior(
    mock_source_get, mock_session_get, mock_repo, client
):
    """Session exists but is not related to the source.

    Intentional behavior change: the router now re-raises HTTPException before
    its broad `except Exception`, so the inner 404 surfaces as a real 404
    instead of being swallowed and re-raised as a 500 embedding the original
    404 message.
    """
    mock_source_get.return_value = _source()
    mock_session_get.return_value = _session()
    mock_repo.return_value = []  # no refers_to relation

    resp = client.get("/api/sources/xyz/chat/sessions/abc")

    assert resp.status_code == 404
    assert resp.json()["detail"] == "Session not found for this source"


@pytest.mark.asyncio
@patch("api.routers._chat_shared.repo_query", new_callable=AsyncMock)
@patch("api.routers._chat_shared.ChatSession.get", new_callable=AsyncMock)
@patch("api.routers._chat_shared.Source.get", new_callable=AsyncMock)
async def test_delete_source_chat_session_missing_relation_behavior(
    mock_source_get, mock_session_get, mock_repo, client
):
    # Intentional behavior change: the inner 404 is no longer swallowed into a
    # 500 by the broad `except Exception` (see the get test above).
    mock_source_get.return_value = _source()
    mock_session_get.return_value = _session()
    mock_repo.return_value = []

    resp = client.delete("/api/sources/xyz/chat/sessions/abc")

    assert resp.status_code == 404
    assert resp.json()["detail"] == "Session not found for this source"


@pytest.mark.asyncio
@patch("api.routers._chat_shared.repo_query", new_callable=AsyncMock)
@patch("api.routers._chat_shared.ChatSession.get", new_callable=AsyncMock)
@patch("api.routers._chat_shared.Source.get", new_callable=AsyncMock)
async def test_send_message_missing_relation_returns_404(
    mock_source_get, mock_session_get, mock_repo, client
):
    """send_message re-raises HTTPException before its broad handler, so the
    missing-relation case surfaces as a real 404 here (unlike get/put/delete)."""
    mock_source_get.return_value = _source()
    mock_session_get.return_value = _session()
    mock_repo.return_value = []

    resp = client.post(
        "/api/sources/xyz/chat/sessions/abc/messages", json={"message": "hi"}
    )

    assert resp.status_code == 404
    assert resp.json()["detail"] == "Session not found for this source"


@pytest.mark.asyncio
@patch("api.routers.source_chat.source_chat_graph")
@patch("api.routers._chat_shared.repo_query", new_callable=AsyncMock)
@patch("api.routers._chat_shared.ChatSession.get", new_callable=AsyncMock)
@patch("api.routers._chat_shared.Source.get", new_callable=AsyncMock)
async def test_get_source_chat_session_happy_path_shapes(
    mock_source_get, mock_session_get, mock_repo, mock_graph, client
):
    mock_source_get.return_value = _source()
    mock_session_get.return_value = _session()
    mock_repo.return_value = [{"in": "chat_session:abc", "out": "source:xyz"}]
    mock_graph.get_state.return_value = _graph_state(
        {
            "messages": [_Msg("m1", "human", "hello"), _Bare()],
            "context_indicators": {"sources": ["source:xyz"], "insights": []},
        }
    )

    resp = client.get("/api/sources/xyz/chat/sessions/abc")

    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == "chat_session:abc"
    assert body["source_id"] == "xyz"
    assert body["message_count"] == 2
    assert body["messages"][0] == {
        "id": "m1",
        "type": "human",
        "content": "hello",
        "timestamp": None,
    }
    assert body["messages"][1] == {
        "id": "msg_1",
        "type": "unknown",
        "content": "bare-repr",
        "timestamp": None,
    }
    assert body["context_indicators"] == {
        "sources": ["source:xyz"],
        "insights": [],
        "notes": [],
    }


# --- source_chat.py: streaming ------------------------------------------------


async def _collect_events(gen):
    import json

    events = []
    async for chunk in gen:
        assert chunk.startswith("data: ")
        events.append(json.loads(chunk[len("data: ") :]))
    return events


@pytest.mark.asyncio
@patch("api.routers.source_chat.source_chat_graph")
async def test_stream_emits_only_the_new_ai_message(mock_graph):
    """The graph result carries the whole checkpointed history; the stream must
    send only this turn's answer, not every previous AI message (#1393)."""
    from api.routers.source_chat import stream_source_chat_response

    mock_graph.get_state.return_value = _graph_state(
        {
            "messages": [
                _Msg("m1", "human", "first question"),
                _Msg("m2", "ai", "first answer"),
            ]
        }
    )
    mock_graph.invoke.return_value = {
        "messages": [
            _Msg("m1", "human", "first question"),
            _Msg("m2", "ai", "first answer"),
            _Msg("m3", "human", "second question"),
            _Msg("m4", "ai", "second answer"),
        ],
        "context_indicators": {"sources": ["source:xyz"], "insights": [], "notes": []},
    }

    events = await _collect_events(
        stream_source_chat_response("chat_session:abc", "source:xyz", "second question")
    )

    ai_events = [e for e in events if e["type"] == "ai_message"]
    assert [e["content"] for e in ai_events] == ["second answer"]
    assert [e["type"] for e in events] == [
        "user_message",
        "history_selection",
        "ai_message",
        "context_indicators",
        "complete",
    ]
    report = next(e for e in events if e["type"] == "history_selection")
    assert report["data"]["kept_exchanges"] >= 1
    assert report["data"]["total_exchanges"] >= report["data"]["kept_exchanges"]


# --- transcript sync state ---------------------------------------------------
#
# The session list carries two flags per session: bytes held here, and the
# manifest naming exactly those bytes. Both computed at read time, never
# stored, so there is no state to go stale - and neither claims the other
# device has anything.


class TestTranscriptSyncState:
    @pytest.mark.asyncio
    async def test_empty_conversation_counts_as_both(self):
        from api.routers._chat_shared import transcript_sync_state

        with patch(
            "api.routers._chat_shared.repo_query", new=AsyncMock(return_value=[])
        ):
            assert await transcript_sync_state("chat_session:empty") == (True, True)

    @pytest.mark.asyncio
    async def test_unstored_transcript_is_neither(self, tmp_path, monkeypatch):
        import open_notebook.sync.exchange as exchange
        from api.routers._chat_shared import transcript_sync_state

        monkeypatch.setattr(exchange, "sync_dir", lambda: tmp_path)
        rows = [
            {
                "id": "chat_message:1",
                "seq": 0,
                "role": "human",
                "content": "q",
                "created": "2026-10-14T00:00:00+00:00",
            }
        ]
        with patch(
            "api.routers._chat_shared.repo_query", new=AsyncMock(return_value=rows)
        ):
            assert await transcript_sync_state("chat_session:x") == (False, False)

    @pytest.mark.asyncio
    async def test_stored_but_unexported(self, tmp_path, monkeypatch):
        import open_notebook.sync.exchange as exchange
        from api.routers._chat_shared import transcript_sync_state
        from open_notebook.storage.blobs import store_bytes
        from open_notebook.sync import chatsync

        monkeypatch.setattr(exchange, "sync_dir", lambda: tmp_path)
        rows = [
            {
                "id": "chat_message:1",
                "seq": 0,
                "role": "human",
                "content": "q",
                "created": "2026-10-14T00:00:00+00:00",
            }
        ]
        data = chatsync.serialize_transcript("chat_session:x", rows)
        store_bytes(data, "chat_session-x.json", root=tmp_path / "blobs")
        monkeypatch.setattr(
            "open_notebook.storage.blobs._root",
            lambda root=None: tmp_path / "blobs",
        )
        with patch(
            "api.routers._chat_shared.repo_query", new=AsyncMock(return_value=rows)
        ):
            assert await transcript_sync_state("chat_session:x") == (True, False)

    @pytest.mark.asyncio
    async def test_exported_when_manifest_names_current_hash(
        self, tmp_path, monkeypatch
    ):
        import hashlib
        import json

        import open_notebook.sync.exchange as exchange
        from api.routers._chat_shared import transcript_sync_state
        from open_notebook.storage.blobs import store_bytes
        from open_notebook.sync import chatsync

        monkeypatch.setattr(exchange, "sync_dir", lambda: tmp_path)
        rows = [
            {
                "id": "chat_message:1",
                "seq": 0,
                "role": "human",
                "content": "q",
                "created": "2026-10-14T00:00:00+00:00",
            }
        ]
        data = chatsync.serialize_transcript("chat_session:x", rows)
        digest = hashlib.sha256(data).hexdigest()
        store_bytes(data, "chat_session-x.json", root=tmp_path / "blobs")
        monkeypatch.setattr(
            "open_notebook.storage.blobs._root",
            lambda root=None: tmp_path / "blobs",
        )
        chat_dir = tmp_path / "chat"
        chat_dir.mkdir(parents=True)
        (chat_dir / "chat_session-x.json").write_text(
            json.dumps({"id": "chat_session:x", "transcript_sha256": digest})
        )
        with patch(
            "api.routers._chat_shared.repo_query", new=AsyncMock(return_value=rows)
        ):
            assert await transcript_sync_state("chat_session:x") == (True, True)

    @pytest.mark.asyncio
    async def test_a_read_failure_never_breaks_listing(self):
        from api.routers._chat_shared import transcript_sync_state

        with patch(
            "api.routers._chat_shared.repo_query",
            new=AsyncMock(side_effect=RuntimeError("db down")),
        ):
            assert await transcript_sync_state("chat_session:x") == (False, False)
