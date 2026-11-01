"""Quota tracking service for cloud processing rate limiting.

Tracks per-provider request counts against configurable limits, with a
unified cumulative budget. Persisted in SurrealDB as a single record
`open_notebook:quota_counters`.

Design principles:
- Default limit is 0 (unlimited) per provider — operators set limits to
  avoid ToS on free tiers.
- Requests are the limiting factor, not tokens or minutes.
- Cumulative budget across all providers: when exhausted, fall back to
  local processing (if enabled) or show a warning.
- All methods are fail-safe: a SurrealDB outage degrades to "allow" so
  the chat path is never blocked by quota infrastructure.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from loguru import logger

from open_notebook.ai.models import QuotaCounters


class QuotaTracker:
    """Rate-limit tracker for cloud processing providers."""

    def __init__(self) -> None:
        self._cache: Optional[QuotaCounters] = None
        self._cache_time: Optional[float] = None

    async def _get(self, ttl_seconds: float = 5.0) -> QuotaCounters:
        """Fetch counters, caching briefly to avoid repeated DB reads."""
        import time

        now = time.monotonic()
        if (
            self._cache is not None
            and self._cache_time is not None
            and now - self._cache_time < ttl_seconds
        ):
            return self._cache
        self._cache = await QuotaCounters.get_instance()
        self._cache_time = now
        return self._cache

    async def can_request(self, provider: str) -> bool:
        """True if a cloud request to `provider` is within quota."""
        try:
            counters = await self._get()
            return counters.can_make_cloud_request(provider)
        except Exception as exc:  # noqa: BLE001 - never block chat on quota
            logger.debug(f"quota check failed for {provider}: {exc}")
            return True  # fail-open

    async def increment(self, provider: str, count: int = 1) -> None:
        """Record `count` requests for a provider."""
        try:
            counters = await self._get()
            counters.increment(provider, count)
            await counters.save()
            self._cache = counters  # refresh cache
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"quota increment failed for {provider}: {exc}")

    async def set_limit(self, provider: str, limit: int, reset_at: Optional[str] = None) -> None:
        """Set the request limit for a provider."""
        try:
            counters = await self._get()
            counters.set_limit(provider, limit, reset_at)
            await counters.save()
            self._cache = counters
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"quota set_limit failed for {provider}: {exc}")

    async def status(self) -> Dict[str, Any]:
        """Return full quota status for the UI."""
        try:
            counters = await self._get()
            providers = {}
            for name, p in counters.providers.items():
                providers[name] = {
                    "used": int(p.get("requests_used") or 0),
                    "limit": int(p.get("limit") or 0),
                    "remaining": counters.provider_remaining(name),
                    "exceeded": counters.provider_exceeded(name),
                    "reset_at": p.get("reset_at"),
                    "last_used_at": p.get("last_used_at"),
                }
            return {
                "providers": providers,
                "cumulative_used": counters.cumulative_used,
                "cumulative_limit": counters.cumulative_limit,
                "cumulative_remaining": counters.cumulative_remaining(),
                "cumulative_exceeded": counters.cumulative_exceeded(),
                "updated_at": counters.updated_at,
            }
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"quota status failed: {exc}")
            return {
                "providers": {},
                "cumulative_used": 0,
                "cumulative_limit": 0,
                "cumulative_remaining": 0,
                "cumulative_exceeded": False,
                "updated_at": None,
            }

    async def reset_provider(self, provider: str) -> None:
        """Reset a provider's counter (used after manual re-login)."""
        try:
            counters = await self._get()
            counters.providers.pop(provider, None)
            counters.cumulative_used = sum(
                int(v.get("requests_used") or 0) for v in counters.providers.values()
            )
            await counters.save()
            self._cache = counters
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"quota reset failed for {provider}: {exc}")


# Singleton instance
quota_tracker = QuotaTracker()
