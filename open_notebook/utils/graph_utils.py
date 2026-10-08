import asyncio
import concurrent.futures
from typing import Any, Coroutine, TypeVar

from langchain_core.messages import BaseMessage, RemoveMessage
from langchain_core.runnables import RunnableConfig
from loguru import logger

T = TypeVar("T")


def run_coro_sync(coro: Coroutine[Any, Any, T]) -> T:
    """Run a coroutine to completion from synchronous code.

    Containment helper for the sync-in-async spots (B1): the LangGraph nodes
    are synchronous and cannot await. Two cases:

    * No loop running on this thread -> ``asyncio.run(coro)``.
    * A loop is already running on this thread -> the coroutine cannot be run
      here, and the caller cannot be made async without the full rewrite that
      is explicitly out of scope, so it runs on a worker thread with its own
      fresh event loop (never touching the caller's loop).

    Exceptions propagate to the caller exactly as ``asyncio.run`` would raise
    them.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    result: list[T] = []

    def _runner() -> None:
        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
            result.append(loop.run_until_complete(coro))
        finally:
            loop.close()
            asyncio.set_event_loop(None)

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        executor.submit(_runner).result()
    return result[0]


async def get_session_message_count(graph, session_id: str) -> int:
    """Get message count from LangGraph state, returns 0 on error."""
    try:
        # Use sync get_state() in a thread (SqliteSaver doesn't support async)
        thread_state = await asyncio.to_thread(
            graph.get_state,
            config=RunnableConfig(configurable={"thread_id": session_id}),
        )
        if thread_state and thread_state.values and "messages" in thread_state.values:
            return len(thread_state.values["messages"])
    except Exception as e:
        logger.warning(f"Could not fetch message count for session {session_id}: {e}")
    return 0


def discard_unanswered_message(graph, thread_id: str, message: BaseMessage) -> None:
    """Remove a user message whose turn failed from the thread's checkpoint.

    LangGraph checkpoints the graph input before running the nodes, so when the
    model call fails the question stays in history with no answer, and a retry
    adds it a second time. Synchronous (SqliteSaver). Best-effort: never raises,
    so it can't mask the original error.
    """
    if not message.id:
        return
    try:
        config = RunnableConfig(configurable={"thread_id": thread_id})
        state = graph.get_state(config)
        messages = (state.values or {}).get("messages", []) if state else []
        if any(getattr(m, "id", None) == message.id for m in messages):
            graph.update_state(config, {"messages": [RemoveMessage(id=message.id)]})
    except Exception:
        logger.exception(f"Could not discard unanswered message in {thread_id}")


def invoke_chat_turn(
    graph, state: dict, config: RunnableConfig, user_message: BaseMessage
) -> Any:
    """Run one chat turn; if it fails, drop the unanswered question.

    Synchronous on purpose: call it via asyncio.to_thread so the invoke and the
    rollback run together in the worker thread, even if the request task is
    cancelled while waiting (client disconnect).
    """
    try:
        # LangGraph accepts a partial state dict at runtime, but its typed
        # signature requires the full state type.
        return graph.invoke(input=state, config=config)
    except Exception:
        thread_id = config.get("configurable", {}).get("thread_id")
        if thread_id:
            discard_unanswered_message(graph, thread_id, user_message)
        raise
