"""DeepSeek-web direct API service — session-cookie based, no browser needed.

Calls chat.deepseek.com's internal web API directly using session credentials
obtained from logging into chat.deepseek.com in a browser.

Requires two tokens from the user's DeepSeek web session:
1. ds_session_id — cookie value from chat.deepseek.com
2. authorization_token — Bearer token from request headers in DevTools

No Chromium, no Playwright, no browser-use — just HTTP requests with the
user's session cookies. Works on low-RAM devices (Snapdragon 439, 3 GB RAM).

If DeepSeek requires a Proof-of-Work challenge (x-ds-pow-response header),
direct mode cannot solve it yet. Use the PC browser-use fallback instead:
run `onb-stack up` on your PC with browser-use installed, or use the
browser-use service on any x86 machine.

WARNING: This reverse-engineers DeepSeek's web interface and may violate
DeepSeek's Terms of Service. Use at your own risk.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Optional

import requests
from loguru import logger

DEEPSEEK_BASE_URL = "https://chat.deepseek.com"

# Headers that the DeepSeek web frontend sends with every request.
_BASE_HEADERS = {
    "accept": "*/*",
    "accept-language": "en-US,en;q=0.9",
    "content-type": "application/json",
    "origin": DEEPSEEK_BASE_URL,
    "referer": DEEPSEEK_BASE_URL + "/",
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-origin",
    "x-app-version": "20241129.1",
    "x-client-locale": "en_US",
    "x-client-platform": "web",
    "x-client-version": "1.5.0",
    "user-agent": "Mozilla/5.0 (X11; Linux x86_64; rv:109.0) Gecko/20100101 Firefox/115.0",
}


class DeepSeekDirectService:
    """Direct API client for DeepSeek-web using session credentials.

    Attempts to call the completion endpoint directly. If DeepSeek requires
    a Proof-of-Work challenge response (x-ds-pow-response header), the request
    will fail with a clear error message telling the user to use the
    browser-use fallback or solve the PoW challenge.
    """

    def __init__(
        self,
        ds_session_id: Optional[str] = None,
        authorization_token: Optional[str] = None,
    ):
        self.ds_session_id = ds_session_id
        self.authorization_token = authorization_token
        self._session: Optional[requests.Session] = None
        self._chat_session_id: Optional[str] = None

    def _get_session(self) -> requests.Session:
        if self._session is None:
            sess = requests.Session()
            sess.headers.update(_BASE_HEADERS)
            if self.ds_session_id:
                sess.cookies.set("ds_session_id", self.ds_session_id)
            if self.authorization_token:
                sess.headers["authorization"] = self.authorization_token
            self._session = sess
        return self._session

    def _ensure_chat_session(self) -> bool:
        """Create a chat session on DeepSeek's web backend."""
        if self._chat_session_id:
            return True
        try:
            sess = self._get_session()
            url = f"{DEEPSEEK_BASE_URL}/api/v0/chat_session/create"
            resp = sess.post(url, data="{}", timeout=30)
            if resp.status_code == 200:
                data: Dict[str, Any] = resp.json()
                if data.get("code") == 0:
                    self._chat_session_id = data["data"]["biz_data"]["id"]
                    return True
            logger.warning(
                f"Failed to create DeepSeek chat session: HTTP {resp.status_code}"
            )
            return False
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"DeepSeek chat session creation error: {exc}")
            return False

    def send_message(
        self,
        message: str,
        thinking_enabled: bool = False,
        timeout: float = 120.0,
    ) -> Dict[str, Any]:
        """Send a message to DeepSeek-web and return the response.

        Returns a dict with keys:
          - response: str | None — the AI response text
          - error: str | None — error message if any
          - manual_login_required: bool — session credentials may be invalid
          - rate_limited: bool — DeepSeek rate-limited the request
          - pow_required: bool — Proof-of-Work challenge is required
        """
        if not self.ds_session_id or not self.authorization_token:
            return {
                "response": None,
                "error": (
                    "DeepSeek-web session credentials not configured. "
                    "Log in to chat.deepseek.com in a browser, then enter "
                    "ds_session_id and authorization_token in Settings."
                ),
                "manual_login_required": True,
                "rate_limited": False,
                "pow_required": False,
            }

        # 1. Ensure chat session exists
        if not self._ensure_chat_session():
            return {
                "response": None,
                "error": "Cannot create DeepSeek chat session — session may be expired.",
                "manual_login_required": True,
                "rate_limited": False,
                "pow_required": False,
            }

        # 2. Call completion endpoint directly (no PoW for now — try without first)
        try:
            sess = self._get_session()
            url = f"{DEEPSEEK_BASE_URL}/api/v0/chat/completion"

            headers = {
                "accept": "text/event-stream",
                "referrer": f"{DEEPSEEK_BASE_URL}/a/chat/s/{self._chat_session_id}",
            }

            data: Dict[str, Any] = {
                "chat_session_id": self._chat_session_id,
                "parent_message_id": None,
                "model_type": "default",
                "prompt": message,
                "ref_file_ids": [],
                "thinking_enabled": thinking_enabled,
                "search_enabled": False,
                "action": None,
                "preempt": False,
            }

            resp = sess.post(
                url,
                headers=headers,
                data=json.dumps(data),
                timeout=timeout,
                stream=True,
            )

            if resp.status_code == 401:
                return {
                    "response": None,
                    "error": "DeepSeek-web session expired or invalid credentials.",
                    "manual_login_required": True,
                    "rate_limited": False,
                    "pow_required": False,
                }

            if resp.status_code == 429:
                return {
                    "response": None,
                    "error": "DeepSeek-web rate limited.",
                    "manual_login_required": False,
                    "rate_limited": True,
                    "pow_required": False,
                }

            if resp.status_code != 200:
                # Check if PoW is required (DeepSeek may return a PoW challenge)
                body = resp.text[:500]
                if "pow" in body.lower() or "proof" in body.lower():
                    return {
                        "response": None,
                        "error": (
                            "DeepSeek-web requires Proof-of-Work challenge response. "
                            "This is not yet supported in direct mode. "
                            "Use browser-use fallback or solve the PoW challenge."
                        ),
                        "manual_login_required": False,
                        "rate_limited": False,
                        "pow_required": True,
                    }
                return {
                    "response": None,
                    "error": f"DeepSeek-web error: HTTP {resp.status_code} — {body}",
                    "manual_login_required": False,
                    "rate_limited": False,
                    "pow_required": False,
                }

            # 3. Parse Server-Sent Events (SSE) response
            content_type = resp.headers.get("content-type", "")
            if "text/event-stream" not in content_type:
                body = resp.text[:500]
                return {
                    "response": None,
                    "error": f"Unexpected response format: {body}",
                    "manual_login_required": False,
                    "rate_limited": False,
                    "pow_required": False,
                }

            response_parts = []
            current_event = None

            for line in resp.iter_lines(decode_unicode=True):
                if not line:
                    continue
                if line.startswith("event: "):
                    current_event = line[7:]
                elif line.startswith("data: "):
                    try:
                        payload = json.loads(line[6:])
                    except json.JSONDecodeError:
                        continue

                    if current_event == "update_session" and isinstance(payload, dict):
                        # Extract text content from SSE data
                        text = self._extract_text(payload)
                        if text:
                            response_parts.append(text)
                    elif current_event in ("finish", "close"):
                        break

            response_text = "".join(response_parts).strip()

            if not response_text:
                return {
                    "response": None,
                    "error": "Empty response from DeepSeek-web.",
                    "manual_login_required": False,
                    "rate_limited": False,
                    "pow_required": False,
                }

            return {
                "response": response_text,
                "error": None,
                "manual_login_required": False,
                "rate_limited": False,
                "pow_required": False,
            }

        except requests.Timeout:
            return {
                "response": None,
                "error": f"DeepSeek-web request timed out after {timeout}s.",
                "manual_login_required": False,
                "rate_limited": False,
                "pow_required": False,
            }
        except Exception as exc:  # noqa: BLE001
            logger.error(f"DeepSeek-direct error: {exc}")
            return {
                "response": None,
                "error": f"DeepSeek-direct service error: {exc}",
                "manual_login_required": False,
                "rate_limited": False,
                "pow_required": False,
            }

    @staticmethod
    def _extract_text(payload: dict) -> str:
        """Extract text content from an SSE data payload dict."""
        if not isinstance(payload, dict):
            return ""
        # DeepSeek SSE wraps content in various shapes; walk common keys.
        if "v" in payload and len(payload) == 1:
            inner = payload["v"]
            if isinstance(inner, str):
                return inner
            if isinstance(inner, list):
                return "".join(
                    DeepSeekDirectService._extract_text(item)
                    if isinstance(item, dict)
                    else str(item)
                    for item in inner
                )
        if "response" in payload and len(payload) == 1:
            return DeepSeekDirectService._extract_text(payload["response"])
        if "type" in payload and payload["type"] in ("THINK", "RESPONSE"):
            if "content" in payload:
                content = payload["content"]
                if isinstance(content, str):
                    return content
                if isinstance(content, list):
                    return "".join(
                        DeepSeekDirectService._extract_text(item)
                        if isinstance(item, dict)
                        else str(item)
                        for item in content
                    )
        return ""


# Singleton instance (credentials set via configure())
deepseek_direct_service = DeepSeekDirectService()


def configure(ds_session_id: str, authorization_token: str) -> None:
    """Configure the singleton with session credentials."""
    deepseek_direct_service.ds_session_id = ds_session_id
    deepseek_direct_service.authorization_token = authorization_token
    deepseek_direct_service._session = None  # Force new session on next use
    deepseek_direct_service._chat_session_id = None
