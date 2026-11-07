"""Browser AI Mode integration service (D17).

Semi-automated browser AI integration with strict rate limiting to avoid
ToS violations. Two providers:

1. **Google AI Mode** — one continuous infinite session per guest/incognito
   web search, wired to one ON++ session, no session series/continuation.
   Integration via API wrapper (`XD06/google-ai-mode` — local OpenAI-compatible
   API, curl_cffi fast path + Chrome CDP fallback). Semi-automated, rate-limited.

2. **Duck.ai** — guest multiple sessions. Integration via unofficial bridges
   (`p2d-duck`, `czpGhost/DuckAI2API`). Rate limits: ~20 req/60s, 1s between
   requests, daily anonymous limit. Single active request at a time.

Q1 answer: API wrapper (semi-automated), not full Playwright. Playwright only
as last-resort fallback. Human-like delays, max 1-2 concurrent requests,
queue + backoff.
"""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass
from typing import Any, Optional

from loguru import logger

#: Duck.ai rate limit: ~20 req per 60s window.
_DUCKAI_WINDOW_SECONDS = 60
_DUCKAI_MAX_PER_WINDOW = 20
_DUCKAI_MIN_INTERVAL = 1.0  # 1s between requests

#: Google AI Mode rate limit: more permissive but still rate-limited.
_GOOGLE_AI_MODE_MIN_INTERVAL = 2.0


@dataclass
class BrowserAIResult:
    """Outcome of a browser AI request."""

    ok: bool
    response: Optional[str] = None
    provider: Optional[str] = None  # google-ai-mode | duck-ai
    session_token: Optional[str] = None  # Google AI Mode continuation token
    error: Optional[str] = None
    rate_limited: bool = False


class RateLimiter:
    """Token-bucket rate limiter with per-provider windows."""

    def __init__(self) -> None:
        self._duckai_requests: list[float] = []
        self._duckai_lock = asyncio.Lock()
        self._google_ai_mode_last: float = 0.0
        self._google_ai_mode_lock = asyncio.Lock()

    async def acquire_duckai(self) -> bool:
        """Return True if a Duck.ai request may proceed now.

        Enforces ~20 req/60s and 1s between requests.
        """
        async with self._duckai_lock:
            now = time.monotonic()
            # Drop requests outside the window.
            self._duckai_requests = [
                t for t in self._duckai_requests if now - t < _DUCKAI_WINDOW_SECONDS
            ]
            if len(self._duckai_requests) >= _DUCKAI_MAX_PER_WINDOW:
                return False
            # Enforce minimum interval between requests.
            if self._duckai_requests:
                elapsed = now - self._duckai_requests[-1]
                if elapsed < _DUCKAI_MIN_INTERVAL:
                    await asyncio.sleep(_DUCKAI_MIN_INTERVAL - elapsed)
                    now = time.monotonic()
            self._duckai_requests.append(now)
            return True

    async def acquire_google_ai_mode(self) -> bool:
        """Return True if a Google AI Mode request may proceed now."""
        async with self._google_ai_mode_lock:
            now = time.monotonic()
            elapsed = now - self._google_ai_mode_last
            if elapsed < _GOOGLE_AI_MODE_MIN_INTERVAL:
                await asyncio.sleep(_GOOGLE_AI_MODE_MIN_INTERVAL - elapsed)
            self._google_ai_mode_last = time.monotonic()
            return True


# Module-level singleton rate limiter.
_rate_limiter = RateLimiter()


class BrowserAIService:
    """Singleton managing Google AI Mode and Duck.ai integration."""

    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}
        self._locks_lock = asyncio.Lock()

    async def _get_lock(self, key: str) -> asyncio.Lock:
        async with self._locks_lock:
            lock = self._locks.get(key)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[key] = lock
            return lock

    # ------------------------------------------------------------------
    # Google AI Mode
    # ------------------------------------------------------------------

    async def google_ai_mode(
        self,
        prompt: str,
        *,
        session_token: Optional[str] = None,
        country: str = "us",
        language: str = "en",
    ) -> BrowserAIResult:
        """Send a prompt to Google AI Mode via API wrapper.

        One continuous infinite session per guest/incognito — no session
        series/continuation. The `session_token` from a previous response
        continues the conversation with prior context.

        Integration via `XD06/google-ai-mode` (local OpenAI-compatible API,
        curl_cffi fast path + Chrome CDP fallback).
        """
        await _rate_limiter.acquire_google_ai_mode()

        lock = await self._get_lock("google-ai-mode")
        async with lock:
            try:
                result = await self._call_google_ai_mode_api(
                    prompt=prompt,
                    session_token=session_token,
                    country=country,
                    language=language,
                )
                return result
            except Exception as exc:  # noqa: BLE001
                logger.error(f"Google AI Mode error: {exc}")
                return BrowserAIResult(
                    ok=False,
                    provider="google-ai-mode",
                    error=str(exc),
                    session_token=session_token,
                )

    async def _call_google_ai_mode_api(
        self,
        prompt: str,
        *,
        session_token: Optional[str],
        country: str,
        language: str,
    ) -> BrowserAIResult:
        """Call the Google AI Mode API wrapper.

        Tries the local XD06/google-ai-mode wrapper first (curl_cffi fast
        path), falls back to Chrome CDP via Playwright as last resort.
        """
        # Option 1: Local API wrapper (XD06/google-ai-mode).
        # The wrapper exposes a local OpenAI-compatible endpoint.
        ai_mode_url = os.getenv("GOOGLE_AI_MODE_URL")
        if ai_mode_url:
            try:
                import httpx

                payload: dict[str, Any] = {
                    "prompt": prompt,
                    "country": country,
                    "language": language,
                }
                if session_token:
                    payload["session_token"] = session_token

                async with httpx.AsyncClient(timeout=120.0) as client:
                    response = await client.post(
                        f"{ai_mode_url.rstrip('/')}/v1/web/ai-mode",
                        json=payload,
                    )
                if response.status_code == 200:
                    data = response.json()
                    reply_parts = data.get("reply_parts", [])
                    response_text = self._extract_reply_parts(reply_parts)
                    new_token = data.get("session_token") or session_token
                    return BrowserAIResult(
                        ok=True,
                        response=response_text,
                        provider="google-ai-mode",
                        session_token=new_token,
                    )
                if response.status_code == 429:
                    return BrowserAIResult(
                        ok=False,
                        provider="google-ai-mode",
                        error="Google AI Mode rate limited",
                        rate_limited=True,
                        session_token=session_token,
                    )
                return BrowserAIResult(
                    ok=False,
                    provider="google-ai-mode",
                    error=f"Google AI Mode API returned {response.status_code}",
                    session_token=session_token,
                )
            except ImportError:
                logger.info("httpx not available for Google AI Mode API wrapper")
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"Google AI Mode API wrapper failed: {exc}")

        # Option 2: Playwright/Chrome CDP fallback (last resort).
        return await self._call_google_ai_mode_playwright(
            prompt=prompt,
            session_token=session_token,
        )

    async def _call_google_ai_mode_playwright(
        self,
        prompt: str,
        *,
        session_token: Optional[str],
    ) -> BrowserAIResult:
        """Last-resort: Google AI Mode via Playwright browser automation.

        Only used when the API wrapper is unavailable. Human-like delays
        and strict rate limiting apply.
        """
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            return BrowserAIResult(
                ok=False,
                provider="google-ai-mode",
                error=(
                    "Google AI Mode unavailable: no API wrapper configured "
                    "(GOOGLE_AI_MODE_URL) and Playwright not installed"
                ),
                session_token=session_token,
            )

        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                try:
                    page = await browser.new_page()
                    await page.goto(
                        "https://google.com/search?udm=14",
                        wait_until="domcontentloaded",
                    )
                    # TODO: Implement actual Google AI Mode interaction via
                    # Playwright. This requires navigating to AI Mode,
                    # entering the prompt, waiting for the response, and
                    # extracting the session_token. Out of scope for P2
                    # initial implementation — API wrapper is the primary path.
                    await browser.close()
                    return BrowserAIResult(
                        ok=False,
                        provider="google-ai-mode",
                        error=(
                            "Google AI Mode Playwright fallback not yet "
                            "implemented — configure GOOGLE_AI_MODE_URL"
                        ),
                        session_token=session_token,
                    )
                finally:
                    if not browser.is_closed():
                        await browser.close()
        except Exception as exc:  # noqa: BLE001
            return BrowserAIResult(
                ok=False,
                provider="google-ai-mode",
                error=f"Playwright error: {exc}",
                session_token=session_token,
            )

    @staticmethod
    def _extract_reply_parts(reply_parts: list[dict]) -> str:
        """Extract text from Google AI Mode reply_parts."""
        parts = []
        for part in reply_parts:
            ptype = part.get("type", "")
            if ptype == "paragraph":
                parts.append(part.get("text", ""))
            elif ptype == "heading":
                parts.append(f"# {part.get('text', '')}")
            elif ptype == "list":
                items = part.get("items", [])
                for item in items:
                    parts.append(f"- {item}")
        return "\n".join(parts).strip()

    # ------------------------------------------------------------------
    # Duck.ai
    # ------------------------------------------------------------------

    async def duck_ai(
        self,
        prompt: str,
        *,
        model: Optional[str] = None,
    ) -> BrowserAIResult:
        """Send a prompt to Duck.ai via unofficial bridge.

        Guest multiple sessions — no continuous session. Rate-limited:
        ~20 req/60s, 1s between, single active request at a time.

        Integration via `p2d-duck` (PyPI) or `amirkabiri/duckai`
        (OpenAI-compatible API server).
        """
        await _rate_limiter.acquire_duckai()

        lock = await self._get_lock("duck-ai")
        async with lock:
            try:
                result = await self._call_duck_ai_api(
                    prompt=prompt,
                    model=model,
                )
                return result
            except Exception as exc:  # noqa: BLE001
                logger.error(f"Duck.ai error: {exc}")
                return BrowserAIResult(
                    ok=False,
                    provider="duck-ai",
                    error=str(exc),
                )

    async def _call_duck_ai_api(
        self,
        prompt: str,
        *,
        model: Optional[str],
    ) -> BrowserAIResult:
        """Call the Duck.ai API via unofficial bridge.

        Tries p2d-duck (PyPI Python client) first, then the
        amirkabiri/duckai OpenAI-compatible server.
        """
        # Option 1: p2d-duck PyPI client.
        import importlib.util

        if importlib.util.find_spec("p2d_duck") is not None:
            try:
                # p2d-duck is a sync client; run in a thread.
                response_text = await asyncio.to_thread(
                    self._p2d_duck_send, prompt, model
                )
                return BrowserAIResult(
                    ok=True,
                    response=response_text,
                    provider="duck-ai",
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"p2d-duck failed: {exc}")
        else:
            logger.info("p2d-duck not installed")

        # Option 2: amirkabiri/duckai OpenAI-compatible server.
        duck_ai_url = os.getenv("DUCK_AI_URL")
        if duck_ai_url:
            try:
                import httpx

                payload: dict[str, Any] = {
                    "model": model or "gpt-4o-mini",
                    "messages": [{"role": "user", "content": prompt}],
                }
                async with httpx.AsyncClient(timeout=120.0) as client:
                    response = await client.post(
                        f"{duck_ai_url.rstrip('/')}/v1/chat/completions",
                        json=payload,
                    )
                if response.status_code == 200:
                    data = response.json()
                    choices = data.get("choices", [])
                    if choices:
                        message = choices[0].get("message", {})
                        return BrowserAIResult(
                            ok=True,
                            response=message.get("content", ""),
                            provider="duck-ai",
                        )
                if response.status_code == 429:
                    return BrowserAIResult(
                        ok=False,
                        provider="duck-ai",
                        error="Duck.ai rate limited",
                        rate_limited=True,
                    )
                return BrowserAIResult(
                    ok=False,
                    provider="duck-ai",
                    error=f"Duck.ai server returned {response.status_code}",
                )
            except ImportError:
                logger.info("httpx not available for Duck.ai server")
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"Duck.ai server failed: {exc}")

        return BrowserAIResult(
            ok=False,
            provider="duck-ai",
            error=(
                "Duck.ai unavailable: p2d-duck not installed and no "
                "DUCK_AI_URL server configured"
            ),
        )

    @staticmethod
    def _p2d_duck_send(prompt: str, model: Optional[str]) -> str:
        """Send via p2d-duck sync client (runs in a thread)."""
        import p2d_duck  # type: ignore

        client = p2d_duck.Client()
        if model:
            response = client.chat(prompt, model=model)
        else:
            response = client.chat(prompt)
        return response.text if hasattr(response, "text") else str(response)


# Module-level singleton.
browser_ai_service = BrowserAIService()
