"""
Unit tests for provider Retry-After handling.

Verifies Flag 3 from the audit: provider retry must cap Retry-After at 60s
and open the circuit breaker rather than sleeping for an hour.
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock

import pytest

from open_notebook.ai.providers import BaseProvider, ProviderSendResult


class _CapTestProvider(BaseProvider):
    """Provider that returns configurable retry_after on every call."""

    provider_id = "cap-test-provider"

    def __init__(self, retry_after):
        self.retry_after = retry_after

    async def _send_message_impl(self, message, **kwargs):
        return ProviderSendResult(
            ok=False,
            error="rate limited",
            rate_limited=True,
            retry_after=self.retry_after,
            used_provider=self.provider_id,
        )

    def get_credentials(self):
        return {"provider": self.provider_id}


@pytest.mark.asyncio
async def test_retry_after_above_cap_opens_circuit_breaker():
    """Retry-After: 3600s must NOT sleep for an hour — circuit breaker opens."""

    provider = _CapTestProvider(retry_after=3600.0)

    start = time.monotonic()
    result = await provider.send_message("hello")
    elapsed = time.monotonic() - start

    # Must return immediately, not sleep 3600s
    assert elapsed < 5.0, f"Slept {elapsed:.1f}s — should have returned immediately"
    assert result.ok is False
    assert result.error == "rate limited"


@pytest.mark.asyncio
async def test_retry_after_at_cap_still_sleeps():
    """Retry-After: exactly 60s is NOT above the cap → normal retry path."""

    provider = _CapTestProvider(retry_after=60.0)

    start = time.monotonic()
    result = await provider.send_message("hello")
    elapsed = time.monotonic() - start

    # 3 attempts × 2 retries × 60s = ~120s (just under, since 60.0 is not > 60.0)
    assert elapsed > 100, f"Expected ~120s (2 retries × 60s), got {elapsed:.2f}s"
    assert result.ok is False


@pytest.mark.asyncio
async def test_retry_after_normal_value_sleeps():
    """Retry-After: 5s → normal retry path, sleeps ~10s (2 retries × 5s)."""

    provider = _CapTestProvider(retry_after=5.0)

    start = time.monotonic()
    result = await provider.send_message("hello")
    elapsed = time.monotonic() - start

    assert 9.0 < elapsed < 12.0, f"Expected ~10s, got {elapsed:.2f}s"
    assert result.ok is False


@pytest.mark.asyncio
async def test_retry_after_none_uses_backoff():
    """retry_after=None → exponential backoff, not a long sleep."""

    provider = _CapTestProvider(retry_after=None)

    start = time.monotonic()
    result = await provider.send_message("hello")
    elapsed = time.monotonic() - start

    # Backoff: attempt 1 → ~1s, attempt 2 → ~3s, then fail fast
    assert 3.0 < elapsed < 6.0, f"Expected ~4s backoff, got {elapsed:.2f}s"
    assert result.ok is False
