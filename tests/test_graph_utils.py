"""Tests for graph_utils.run_coro_sync (B1 sync-in-async containment helper)."""

import asyncio

import pytest

from open_notebook.utils.graph_utils import run_coro_sync


def test_runs_without_running_loop():
    async def coro():
        await asyncio.sleep(0)
        return 42

    assert run_coro_sync(coro()) == 42


def test_propagates_exception_without_running_loop():
    async def coro():
        raise ValueError("boom")

    with pytest.raises(ValueError, match="boom"):
        run_coro_sync(coro())


def test_returns_value_inside_running_loop():
    async def coro():
        await asyncio.sleep(0)
        return "ok"

    async def main():
        # Sync call from inside a running loop: must not raise and must return.
        return run_coro_sync(coro())

    assert asyncio.run(main()) == "ok"


def test_propagates_exception_inside_running_loop():
    async def coro():
        raise RuntimeError("inner-boom")

    async def main():
        return run_coro_sync(coro())

    with pytest.raises(RuntimeError, match="inner-boom"):
        asyncio.run(main())


def test_runs_on_a_separate_loop_and_closes_it():
    captured = {}

    async def coro():
        captured["loop"] = asyncio.get_running_loop()
        return 7

    async def main():
        captured["caller_loop"] = asyncio.get_running_loop()
        return run_coro_sync(coro())

    assert asyncio.run(main()) == 7
    # The coroutine ran on its own worker loop, not the caller's...
    assert captured["loop"] is not captured["caller_loop"]
    # ...and that worker loop was closed (no leak).
    assert captured["loop"].is_closed()


def test_caller_loop_survives_the_call():
    async def coro():
        return 1

    async def main():
        run_coro_sync(coro())
        await asyncio.sleep(0)  # caller loop still usable
        return "alive"

    assert asyncio.run(main()) == "alive"
