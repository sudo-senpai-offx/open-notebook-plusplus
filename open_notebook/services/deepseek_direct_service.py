"""DeepSeek-web direct API service — session-cookie based, no browser needed.

Calls chat.deepseek.com's internal web API directly using session credentials
obtained from logging into chat.deepseek.com in a browser.

Requires two tokens from the user's DeepSeek web session:
1. ds_session_id — cookie value from chat.deepseek.com
2. authorization_token — Bearer token from request headers in DevTools

No Chromium, no Playwright, no browser-use — just HTTP requests with the
user's session cookies plus the reverse-engineered ``DeepSeekHashV1``
proof-of-work answer. Works on low-RAM devices (Snapdragon 439, 3 GB RAM),
because the PoW is solved by a small WebAssembly module run through
``wasmtime`` (~0.1 s per challenge).

Flow per message:
1. ``POST /api/v0/chat_session/create`` — one chat session per conversation.
2. ``POST /api/v0/chat/create_pow_challenge`` — challenge for the target path.
3. Solve it, base64-JSON the answer into ``x-ds-pow-response``.
4. ``POST /api/v0/chat/completion`` — SSE stream, reassembled below.

WARNING: This reverse-engineers DeepSeek's web interface and may violate
DeepSeek's Terms of Service. Use at your own risk.
"""

from __future__ import annotations

import base64
import json
from typing import Any, Dict, List, Optional

import requests
from loguru import logger

DEEPSEEK_BASE_URL = "https://chat.deepseek.com"
COMPLETION_PATH = "/api/v0/chat/completion"

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
    "x-client-bundle-id": "com.deepseek.chat",
    "x-client-locale": "en",
    "x-client-platform": "web",
    "x-client-version": "2.7.0",
    "user-agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36"
    ),
}


def _solve_pow(challenge_data: Dict[str, Any]) -> Optional[str]:
    """Solve a ``DeepSeekHashV1`` challenge and return the header value.

    Returns the base64-encoded JSON body expected in ``x-ds-pow-response``,
    or None when the solver is unavailable or the algorithm is unknown.
    """
    try:
        from deepseek_pow import Challenge, registry
    except ImportError:
        logger.warning(
            "deepseek-pow is not installed; DeepSeek PoW cannot be solved. "
            "Install it with: pip install deepseek-pow wasmtime"
        )
        return None

    algorithm = challenge_data.get("algorithm") or "DeepSeekHashV1"
    try:
        challenge = Challenge(
            algorithm=algorithm,
            challenge=challenge_data["challenge"],
            salt=challenge_data["salt"],
            difficulty=challenge_data["difficulty"],
            expire_at=challenge_data["expire_at"],
            signature=challenge_data.get("signature"),
        )
        solution = registry.get(algorithm).solve(challenge)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"DeepSeek PoW solve failed: {exc}")
        return None

    payload = {
        "algorithm": algorithm,
        "challenge": challenge.challenge,
        "salt": challenge.salt,
        "answer": int(solution.answer),
        "expire_at": challenge.expire_at,
        "signature": challenge.signature,
        "target_path": challenge_data.get("target_path", COMPLETION_PATH),
    }
    return base64.b64encode(json.dumps(payload).encode()).decode()


class DeepSeekDirectService:
    """Direct API client for DeepSeek-web using session credentials."""

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
                # A pasted DevTools cookie string ("a=1; b=2") carries more
                # than ds_session_id: aws-waf-token and smidV2 matter too.
                if "=" in self.ds_session_id:
                    for pair in self.ds_session_id.split(";"):
                        name, _, value = pair.strip().partition("=")
                        if name and value:
                            sess.cookies.set(name.strip(), value.strip())
                else:
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
            resp = sess.post(url, json={"character_id": None}, timeout=30)
            if resp.status_code == 200:
                data: Dict[str, Any] = resp.json()
                if data.get("code") == 0:
                    biz = (data.get("data") or {}).get("biz_data") or {}
                    chat_session = biz.get("chat_session") or {}
                    self._chat_session_id = chat_session.get("id")
                    if self._chat_session_id:
                        return True
            logger.warning(
                f"Failed to create DeepSeek chat session: HTTP {resp.status_code}"
            )
            return False
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"DeepSeek chat session creation error: {exc}")
            return False

    def _pow_header(self, timeout: float = 30.0) -> Optional[str]:
        """Fetch a challenge and return the solved ``x-ds-pow-response``."""
        sess = self._get_session()
        resp = sess.post(
            f"{DEEPSEEK_BASE_URL}/api/v0/chat/create_pow_challenge",
            json={"target_path": COMPLETION_PATH},
            timeout=timeout,
        )
        if resp.status_code != 200:
            logger.warning(f"DeepSeek PoW challenge failed: HTTP {resp.status_code}")
            return None
        data = resp.json()
        try:
            challenge_data = data["data"]["biz_data"]["challenge"]
        except (KeyError, TypeError):
            logger.warning(f"DeepSeek PoW challenge shape unexpected: {data}")
            return None
        return _solve_pow(challenge_data)

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
          - pow_required: bool — a Proof-of-Work challenge was issued
          - pow_solved: bool — the challenge was answered
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
                "pow_solved": False,
            }

        # 1. Ensure chat session exists
        if not self._ensure_chat_session():
            return {
                "response": None,
                "error": "Cannot create DeepSeek chat session — session may be expired.",
                "manual_login_required": True,
                "rate_limited": False,
                "pow_required": False,
                "pow_solved": False,
            }

        # 2. Solve the proof-of-work challenge for the completion endpoint
        pow_header = self._pow_header()
        if not pow_header:
            return {
                "response": None,
                "error": (
                    "DeepSeek-web issued a Proof-of-Work challenge that could "
                    "not be solved. Install the solver: pip install deepseek-pow "
                    "wasmtime."
                ),
                "manual_login_required": False,
                "rate_limited": False,
                "pow_required": True,
                "pow_solved": False,
            }

        # 3. Call completion endpoint
        try:
            sess = self._get_session()
            url = f"{DEEPSEEK_BASE_URL}{COMPLETION_PATH}"

            headers = {
                "accept": "*/*",
                "referer": f"{DEEPSEEK_BASE_URL}/a/chat/s/{self._chat_session_id}",
                "x-ds-pow-response": pow_header,
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
                json=data,
                timeout=timeout,
                stream=True,
            )

            if resp.status_code == 401:
                return {
                    "response": None,
                    "error": "DeepSeek-web session expired or invalid credentials.",
                    "manual_login_required": True,
                    "rate_limited": False,
                    "pow_required": True,
                    "pow_solved": True,
                }

            if resp.status_code == 429:
                return {
                    "response": None,
                    "error": "DeepSeek-web rate limited.",
                    "manual_login_required": False,
                    "rate_limited": True,
                    "pow_required": True,
                    "pow_solved": True,
                }

            if resp.status_code != 200:
                return {
                    "response": None,
                    "error": (
                        f"DeepSeek-web error: HTTP {resp.status_code} — "
                        f"{resp.text[:500]}"
                    ),
                    "manual_login_required": False,
                    "rate_limited": False,
                    "pow_required": True,
                    "pow_solved": True,
                }

            content_type = resp.headers.get("content-type", "")
            if "text/event-stream" not in content_type:
                body = resp.text[:800]
                # The web API reports application-level failures as JSON
                # with HTTP 200, e.g. {"code":40300,"msg":"MISSING_HEADER"}.
                return {
                    "response": None,
                    "error": self._describe_json_error(body),
                    "manual_login_required": "invalid chat session" in body
                    or "expired" in body.lower(),
                    "rate_limited": "RATE_LIMIT" in body or "TOO_MANY" in body,
                    "pow_required": True,
                    "pow_solved": True,
                }

            response_text = self._read_sse(resp)

            if not response_text:
                return {
                    "response": None,
                    "error": "Empty response from DeepSeek-web.",
                    "manual_login_required": False,
                    "rate_limited": False,
                    "pow_required": True,
                    "pow_solved": True,
                }

            return {
                "response": response_text,
                "error": None,
                "manual_login_required": False,
                "rate_limited": False,
                "pow_required": True,
                "pow_solved": True,
            }

        except requests.Timeout:
            return {
                "response": None,
                "error": f"DeepSeek-web request timed out after {timeout}s.",
                "manual_login_required": False,
                "rate_limited": False,
                "pow_required": True,
                "pow_solved": True,
            }
        except Exception as exc:  # noqa: BLE001
            logger.error(f"DeepSeek-direct error: {exc}")
            return {
                "response": None,
                "error": f"DeepSeek-direct service error: {exc}",
                "manual_login_required": False,
                "rate_limited": False,
                "pow_required": False,
                "pow_solved": False,
            }

    @staticmethod
    def _describe_json_error(body: str) -> str:
        """Turn DeepSeek's JSON error envelope into a readable message."""
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            return f"Unexpected response format: {body}"
        code = payload.get("code")
        msg = payload.get("msg") or "unknown error"
        biz_msg = ((payload.get("data") or {}).get("biz_msg")) or ""
        detail = ": ".join(part for part in (msg, biz_msg) if part)
        return f"DeepSeek-web rejected the request (code {code}): {detail}"

    @staticmethod
    def _read_sse(resp: requests.Response) -> str:
        """Reassemble the assistant's text from DeepSeek's SSE stream.

        The stream sends a full snapshot once (``{"v": {"response": ...}}``)
        and then incremental patches, e.g.
        ``{"p": "response/fragments/-1/content", "o": "APPEND", "v": "..."}``.
        Only ``RESPONSE`` fragments are kept; ``THINK`` fragments are the
        model's reasoning and are not part of the answer.
        """
        fragments: Dict[str, List[str]] = {}
        order: List[str] = []
        response_fragment_ids: set[str] = set()

        def apply_patch(path: str, op: str, value: Any) -> None:
            parts = path.split("/")
            if len(parts) < 3 or parts[0] != "response" or parts[1] != "fragments":
                return
            ref = parts[2]
            if ref == "-1":
                if not order:
                    return
                fid = order[-1]
            else:
                fid = ref
            if fid not in fragments:
                fragments[fid] = [""]
                order.append(fid)
            if not isinstance(value, str):
                return
            if op == "APPEND":
                fragments[fid].append(value)
            else:
                fragments[fid] = [value]

        for raw_line in resp.iter_lines(decode_unicode=True):
            # requests types iter_lines() as bytes even when decode_unicode
            # is set, so normalise here rather than casting at every use.
            line = raw_line.decode() if isinstance(raw_line, bytes) else raw_line
            if not line or not line.startswith("data: "):
                continue
            try:
                payload = json.loads(line[6:])
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict):
                continue

            snapshot = payload.get("v")
            if isinstance(snapshot, dict) and "response" in snapshot:
                inner = snapshot["response"] or {}
                for fragment in inner.get("fragments") or []:
                    if not isinstance(fragment, dict):
                        continue
                    fid = str(fragment.get("id"))
                    if fid not in fragments:
                        order.append(fid)
                    fragments[fid] = [fragment.get("content") or ""]
                    if fragment.get("type") == "RESPONSE":
                        response_fragment_ids.add(fid)

            path = payload.get("p")
            if isinstance(path, str):
                op = payload.get("o") or "SET"
                value = payload.get("v")
                if op == "BATCH" and isinstance(value, list):
                    for item in value:
                        if isinstance(item, dict) and isinstance(item.get("p"), str):
                            apply_patch(
                                item["p"],
                                item.get("o") or "SET",
                                item.get("v"),
                            )
                else:
                    apply_patch(path, op, value)

            if payload.get("p") == "response/status" and payload.get("v") == "FINISHED":
                break

        # When the snapshot never named fragment types, fall back to every
        # fragment: an untyped stream is better than an empty answer.
        selected = response_fragment_ids or set(order)
        return "".join(
            "".join(fragments[fid]) for fid in order if fid in selected
        ).strip()


# Singleton instance (credentials set via configure())
deepseek_direct_service = DeepSeekDirectService()


def configure(ds_session_id: str, authorization_token: str) -> None:
    """Configure the singleton with session credentials."""
    deepseek_direct_service.ds_session_id = ds_session_id
    deepseek_direct_service.authorization_token = authorization_token
    deepseek_direct_service._session = None  # Force new session on next use
    deepseek_direct_service._chat_session_id = None
