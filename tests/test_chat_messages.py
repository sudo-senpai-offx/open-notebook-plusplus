"""Chat transcripts, as records rather than a checkpoint blob.

A conversation used to live only in LangGraph's SQLite checkpoint: an
opaque serialisation keyed by internal channel names, with no union over
two of them. So a session could not cross devices at all - it had to be
retyped.

These pin the two properties the sync depends on: writing is idempotent
by message id, and the record id is the merge key.
"""

import os
import uuid

import pytest
from langchain_core.messages import AIMessage, HumanMessage

pytestmark = pytest.mark.skipif(
    not os.getenv("SURREAL_TEST_URL"),
    reason="needs a live SurrealDB; set SURREAL_TEST_URL",
)


def _msg(role: str, content: str, mid: str):
    message = (
        HumanMessage(content=content) if role == "human" else AIMessage(content=content)
    )
    message.id = mid
    return message


@pytest.fixture
def device(monkeypatch):
    issued = []
    by_label = {}

    def use(label: str) -> str:
        if label not in by_label:
            ns = f"chatmsg_{label}_{uuid.uuid4().hex[:8]}"
            by_label[label] = ns
            issued.append(ns)
        monkeypatch.setenv("SURREAL_NAMESPACE", by_label[label])
        monkeypatch.setenv("SURREAL_DATABASE", by_label[label])
        return by_label[label]

    monkeypatch.setenv("SURREAL_URL", os.environ["SURREAL_TEST_URL"])
    monkeypatch.setenv("SURREAL_USER", os.getenv("SURREAL_TEST_USER", "root"))
    monkeypatch.setenv("SURREAL_PASSWORD", os.getenv("SURREAL_TEST_PASSWORD", "root"))
    use("a")

    yield use

    import asyncio

    from open_notebook.database.repository import repo_query

    async def wipe() -> None:
        for ns in issued:
            monkeypatch.setenv("SURREAL_NAMESPACE", ns)
            monkeypatch.setenv("SURREAL_DATABASE", ns)
            for table in ("chat_message", "chat_session"):
                try:
                    await repo_query(f"DELETE {table}")
                except Exception:
                    pass

    try:
        asyncio.run(wipe())
    except Exception:
        pass


async def _new_session(title: str = "t") -> str:
    from open_notebook.domain.notebook import ChatSession

    session = ChatSession(title=title)
    await session.save()
    return str(session.id)


class TestWrite:
    @pytest.mark.asyncio
    async def test_messages_are_persisted(self, device):
        from open_notebook.domain.notebook import persist_chat_messages

        sid = await _new_session()
        added = await persist_chat_messages(
            sid, [_msg("human", "what is a library", "u1"), _msg("ai", "this", "a1")]
        )

        assert added == 2

    @pytest.mark.asyncio
    async def test_writing_twice_adds_nothing(self, device):
        """The regression.

        The stored id is `chat_message:u1`; comparing the bare key `u1`
        against it never matches, so every turn re-wrote the entire
        transcript and the conversation doubled each time. Found by
        running it against a live database - `CREATE` failed on the
        duplicate id - not by reading it.
        """
        from open_notebook.domain.notebook import persist_chat_messages

        sid = await _new_session()
        messages = [_msg("human", "q", "u1"), _msg("ai", "a", "a1")]

        assert await persist_chat_messages(sid, messages) == 2
        assert await persist_chat_messages(sid, messages) == 0
        assert await persist_chat_messages(sid, messages) == 0

    @pytest.mark.asyncio
    async def test_a_new_turn_appends_only_itself(self, device):
        from open_notebook.domain.notebook import persist_chat_messages

        sid = await _new_session()
        await persist_chat_messages(sid, [_msg("human", "first", "u1")])

        added = await persist_chat_messages(
            sid, [_msg("human", "first", "u1"), _msg("ai", "answer", "a1")]
        )

        assert added == 1, "only the new message should be written"

    @pytest.mark.asyncio
    async def test_a_positional_id_gets_minted(self, device):
        """The response serialiser's `msg_0` fallback is index-based.

        Two devices computing the same transcript would produce the same
        fallback ids for different messages, so they are replaced.
        """
        from open_notebook.domain.notebook import persist_chat_messages

        sid = await _new_session()
        added = await persist_chat_messages(sid, [_msg("human", "q", "msg_0")])

        assert added == 1
        rows = await _query("SELECT id FROM chat_message")
        assert not any(str(r["id"]).endswith("msg_0") for r in rows)

    @pytest.mark.asyncio
    async def test_blank_content_is_not_stored(self, device):
        from open_notebook.domain.notebook import persist_chat_messages

        sid = await _new_session()
        assert await persist_chat_messages(sid, [_msg("human", "   ", "u1")]) == 0


class TestMerge:
    @pytest.mark.asyncio
    async def test_two_devices_appending_produce_a_union(self, device):
        """The whole point: no conflict when both write.

        Overlapping `seq` values are expected - each device numbers
        independently - so `seq` is display order, not a merge key. The
        record id is the key, and nothing is overwritten.
        """
        from open_notebook.database.repository import ensure_record_id
        from open_notebook.domain.notebook import ChatMessage, persist_chat_messages

        sid = await _new_session()
        await persist_chat_messages(
            sid, [_msg("human", "from the phone", "u1"), _msg("ai", "phone", "a1")]
        )
        first = (
            await _query(
                "SELECT * FROM chat_message WHERE session = $s",
                {"s": ensure_record_id(sid)},
            )
        )[0]

        # The other device appends with its own numbering.
        await _create_message(sid, seq=0, role="human", content="from the PC")

        transcript = await ChatMessage(**first).transcript()

        assert len(transcript) == 3
        assert {m.content for m in transcript} == {
            "from the phone",
            "phone",
            "from the PC",
        }

    @pytest.mark.asyncio
    async def test_the_same_message_twice_is_still_one(self, device):
        """A replayed sync must not duplicate."""
        from open_notebook.database.repository import ensure_record_id
        from open_notebook.domain.notebook import ChatMessage, persist_chat_messages

        sid = await _new_session()
        messages = [_msg("human", "same", "u1"), _msg("ai", "reply", "a1")]
        await persist_chat_messages(sid, messages)
        first = (
            await _query(
                "SELECT * FROM chat_message WHERE session = $s",
                {"s": ensure_record_id(sid)},
            )
        )[0]

        await persist_chat_messages(sid, messages)

        transcript = await ChatMessage(**first).transcript()
        assert len(transcript) == 2


class TestDegradation:
    @pytest.mark.asyncio
    async def test_a_message_with_no_session_reads_empty(self, device):
        """`session` is nullable, so a partial row is representable."""
        from open_notebook.domain.notebook import ChatMessage

        assert await ChatMessage(id="chat_message:x", content="hi").transcript() == []

    @pytest.mark.asyncio
    async def test_an_unknown_role_is_normalised_not_rejected(self, device):
        """A role the schema does not allow must not fail a write."""
        from open_notebook.domain.notebook import ChatMessage

        assert ChatMessage(role="tool").role == "system"
        assert ChatMessage(role="ai").role == "ai"


class TestMigration:
    def test_the_table_is_declared(self):
        sql = (
            __import__("pathlib").Path(__file__).resolve().parents[1]
            / "open_notebook/database/migrations/36.surrealql"
        ).read_text()
        assert "DEFINE TABLE IF NOT EXISTS chat_message" in sql
        # A table-level FLEXIBLE is a parse error on SurrealDB 2.7;
        # FLEXIBLE belongs on object fields in this schema.
        assert "chat_message SCHEMAFULL FLEXIBLE" not in sql


async def _query(statement: str, params: dict | None = None):
    from open_notebook.database.repository import repo_query

    return await repo_query(statement, params)


async def _create_message(session_id: str, seq: int, role: str, content: str):
    from uuid import uuid4

    from open_notebook.database.repository import ensure_record_id

    await _query(
        "CREATE $id CONTENT {session: $s, seq: $q, role: $r, content: $c, updated: time::now()}",
        {
            "id": ensure_record_id(f"chat_message:{uuid4()}"),
            "s": ensure_record_id(session_id),
            "q": seq,
            "r": role,
            "c": content,
        },
    )
