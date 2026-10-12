"""Tiered retry budgets (B5). The worker library supports a single attempt
budget; this loop owns per-error-kind counts instead:

- ``DatabaseOperationError`` (SurrealDB transaction conflicts): deep budget.
- Other exceptions: short budget (localhost blips recover fast or not at all).
- ``stop_on`` types + cancellation: immediate, never counted.

Callers set the library retry to a single attempt and keep its logging and
final-status marking; all counting lives here with an injectable clock.
"""

from __future__ import annotations

import asyncio
import random
from typing import Any, Awaitable, Callable, Tuple, Type

from loguru import logger

from open_notebook.exceptions import DatabaseOperationError

Wait = Callable[[float], Awaitable[None]]


async def _default_wait(delay: float) -> None:
    await asyncio.sleep(delay)


def _backoff(base: float, attempt: int, cap: float) -> float:
    return min(cap, base * (2.0 ** max(0, attempt - 1)) + random.uniform(0, 1.0))


async def execute_with_budgets(
    fn: Callable[[], Awaitable[Any]],
    *,
    stop_on: Tuple[Type[BaseException], ...] = (),
    db_attempts: int = 15,
    transient_attempts: int = 3,
    db_base: float = 1.0,
    transient_base: float = 1.0,
    cap: float = 60.0,
    wait: Wait = _default_wait,
) -> Any:
    """Run ``fn`` to success or budget exhaustion. Returns its result."""
    db_used = 0
    transient_used = 0
    while True:
        try:
            return await fn()
        except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
            raise
        except stop_on:
            raise
        except DatabaseOperationError as e:
            db_used += 1
            if db_used >= db_attempts:
                raise
            delay = _backoff(db_base, db_used, cap)
            logger.debug(f"[Retry] DB conflict {db_used}/{db_attempts}: {e}")
            await wait(delay)
        except Exception as e:
            transient_used += 1
            if transient_used >= transient_attempts:
                raise
            delay = _backoff(transient_base, transient_used, cap)
            logger.debug(
                f"[Retry] transient {transient_used}/{transient_attempts}: {e}"
            )
            await wait(delay)
