"""Tiered retry budgets: DB conflicts ride deep, other transients short."""

import asyncio

import pytest

from open_notebook.exceptions import DatabaseOperationError
from open_notebook.utils.retry_budgets import execute_with_budgets


def _waiter(log):
    async def wait(delay):
        log.append(delay)

    return wait


@pytest.mark.asyncio
async def test_success_first_try_no_waits():
    log = []
    calls = []

    async def fn():
        calls.append(1)
        return "ok"

    assert await execute_with_budgets(fn, wait=_waiter(log)) == "ok"
    assert len(calls) == 1 and log == []


@pytest.mark.asyncio
async def test_db_conflicts_ride_full_budget():
    log = []
    calls = []

    async def fn():
        calls.append(1)
        raise DatabaseOperationError("conflict")

    with pytest.raises(DatabaseOperationError):
        await execute_with_budgets(fn, db_attempts=15, wait=_waiter(log))
    assert len(calls) == 15
    assert len(log) == 14


@pytest.mark.asyncio
async def test_transients_capped_low():
    log = []
    calls = []

    async def fn():
        calls.append(1)
        raise ConnectionError("flap")

    with pytest.raises(ConnectionError):
        await execute_with_budgets(fn, transient_attempts=3, wait=_waiter(log))
    assert len(calls) == 3
    assert len(log) == 2


@pytest.mark.asyncio
async def test_permanent_errors_raise_immediately():
    log = []
    calls = []

    async def fn():
        calls.append(1)
        raise ValueError("bad input")

    with pytest.raises(ValueError):
        await execute_with_budgets(fn, stop_on=(ValueError,), wait=_waiter(log))
    assert len(calls) == 1 and log == []


@pytest.mark.asyncio
async def test_cancellation_never_counted():
    log = []
    calls = []

    async def fn():
        calls.append(1)
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await execute_with_budgets(fn, wait=_waiter(log))
    assert len(calls) == 1 and log == []


@pytest.mark.asyncio
async def test_recovery_mid_budget():
    log = []
    calls = []

    async def fn():
        calls.append(1)
        if len(calls) < 3:
            raise DatabaseOperationError("conflict")
        return "recovered"

    assert await execute_with_budgets(fn, wait=_waiter(log)) == "recovered"
    assert len(calls) == 3
