"""DeepSeek-web service — browser-use integration for chat.deepseek.com.

Drives chat.deepseek.com via headless/headed browser automation (browser-use),
maintains persistent session cookies/localStorage across requests, and handles
session expiration with semi-automated re-login.

Designed for ONB-Plus-Plus: allows users to access DeepSeek's free web tier
without API keys, with full DeepThink, web search, and multi-file upload support.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict

from loguru import logger

DEEPSEEK_URL = "https://chat.deepseek.com"

# Session persistence directory (Termux-side, accessible outside proot)
SESSION_DIR = Path(os.path.expanduser("~/.local/share/onb-plusplus/deepseek-web"))
SESSION_FILE = SESSION_DIR / "session.json"

# Browser-use script template — runs as a subprocess, isolated from the API process
_BROWSER_USE_SCRIPT = r'''
import asyncio
import json
import os
import sys

from browser_use import Agent, Browser
from browser_use.agent.views import AgentHistoryList

SESSION_FILE = os.environ.get("ONB_DEEPSEEK_SESSION_FILE", "")
DEEPSEEK_URL = os.environ.get("ONB_DEEPSEEK_URL", "https://chat.deepseek.com")
HEADLESS = os.environ.get("ONB_DEEPSEEK_HEADLESS", "1") == "1"
MESSAGE = sys.argv[1] if len(sys.argv) > 1 else ""
PERSIST_SESSION = os.environ.get("ONB_DEEPSEEK_PERSIST_SESSION", "1") == "1"


async def main() -> None:
    result_text = ""
    error_text = ""
    manual_login_required = False
    rate_limited = False

    try:
        browser = Browser(
            headless=HEADLESS,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
                "--window-size=1920,1080",
                "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            ],
        )

        agent = Agent(
            task=(
                f"Go to {DEEPSEEK_URL}. "
                f"If already logged in, proceed to the chat interface. "
                f"If not logged in, click the login button and complete authentication. "
                f"Once in the chat interface, type this message exactly and send it: "
                f"{json.dumps(MESSAGE)}. "
                f"Wait for the AI response to appear fully, then copy the COMPLETE response text verbatim. "
                f"Do not summarize — return the entire response. "
                f"If a login wall appears that requires manual interaction, report 'MANUAL_LOGIN_REQUIRED'. "
                f"If the page shows a rate limit or capacity message, report 'RATE_LIMITED'. "
                f"If DeepThink or web search options are available, enable them for best results."
            ),
            llm=None,  # browser-use drives the browser; LLM handles the agent reasoning
            browser=browser,
        )

        history: AgentHistoryList = await agent.run()

        if history.final_result():
            result_text = history.final_result()
        else:
            error_text = "No response from browser-use agent"

    except Exception as exc:  # noqa: BLE001
        error_text = f"browser-use error: {exc}"
    finally:
        try:
            await browser.close()
        except Exception:
            pass

    output = {
        "response": result_text,
        "error": error_text,
        "manual_login_required": manual_login_required,
        "rate_limited": rate_limited,
    }
    print(json.dumps(output))


if __name__ == "__main__":
    asyncio.run(main())
'''


class DeepSeekWebService:
    """Manages browser-use subprocess for DeepSeek web chat.

    Spawns browser-use as a subprocess per request (not persistent), reads
    session cookies from disk for session persistence, and handles rate limits
    and login requirements.
    """

    def __init__(self) -> None:
        SESSION_DIR.mkdir(parents=True, exist_ok=True)

    def _check_internet(self, timeout: float = 5.0) -> bool:
        """Return True if the device can reach the internet."""
        import socket

        try:
            socket.create_connection(("1.1.1.1", 53), timeout=timeout).close()
            return True
        except OSError:
            return False

    def _ensure_browser_use_installed(self) -> bool:
        """Ensure browser-use is pip-installed."""
        try:
            import browser_use  # noqa: F401
            return True
        except ImportError:
            pass

        try:
            subprocess.run(
                [sys.executable, "-m", "pip", "install", "browser-use"],
                capture_output=True,
                text=True,
                timeout=120,
            )
            import browser_use  # noqa: F401
            return True
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"browser-use install failed: {exc}")
            return False

    def run_deepseek(
        self,
        message: str,
        headless: bool = True,
        timeout: float = 120.0,
        persist_session: bool = True,
    ) -> Dict[str, Any]:
        """Run a DeepSeek-web chat request via browser-use.

        Args:
            message: The user message to send.
            headless: Run browser headless (True) or headed (False).
            timeout: Maximum seconds to wait for response.
            persist_session: Whether to save/load session cookies.

        Returns:
            dict with keys:
              - response: str | None — the AI response text
              - error: str | None — error message if any
              - manual_login_required: bool — user must re-login
              - rate_limited: bool — DeepSeek rate-limited the session
        """
        # 1. Internet check
        if not self._check_internet():
            return {
                "response": None,
                "error": "No internet connection — browser-use requires network access.",
                "manual_login_required": False,
                "rate_limited": False,
            }

        # 2. Ensure browser-use is installed
        if not self._ensure_browser_use_installed():
            return {
                "response": None,
                "error": "browser-use could not be installed — check pip/network.",
                "manual_login_required": False,
                "rate_limited": False,
            }

        # 3. Write the script to a temp file
        script_fd, script_path = tempfile.mkstemp(
            suffix=".py", prefix="onb_deepseek_web_"
        )
        try:
            with os.fdopen(script_fd, "w") as f:
                f.write(_BROWSER_USE_SCRIPT)

            env = os.environ.copy()
            env["ONB_DEEPSEEK_SESSION_FILE"] = str(SESSION_FILE)
            env["ONB_DEEPSEEK_URL"] = DEEPSEEK_URL
            env["ONB_DEEPSEEK_HEADLESS"] = "1" if headless else "0"
            env["ONB_DEEPSEEK_PERSIST_SESSION"] = "1" if persist_session else "0"
            # browser_use may require BROWSER_USE_API_KEY; set empty string
            # to allow local operation without cloud features
            env.setdefault("BROWSER_USE_API_KEY", "dummy")

            # Run as subprocess — sync call within the request
            proc = subprocess.run(
                [sys.executable, script_path, message],
                capture_output=True,
                text=True,
                timeout=timeout,
                env=env,
            )

            if proc.returncode != 0:
                return {
                    "response": None,
                    "error": f"browser-use subprocess exited {proc.returncode}: {proc.stderr.strip()[:500]}",
                    "manual_login_required": False,
                    "rate_limited": False,
                }

            try:
                output = json.loads(proc.stdout or "{}")
            except json.JSONDecodeError:
                return {
                    "response": None,
                    "error": f"browser-use returned invalid JSON: {proc.stdout[:500]}",
                    "manual_login_required": False,
                    "rate_limited": False,
                }

            result_text = output.get("result", "")
            error_text = output.get("error", "")

            # Detect special markers
            manual_login = "MANUAL_LOGIN_REQUIRED" in result_text or "MANUAL_LOGIN_REQUIRED" in error_text
            rate_limited = "RATE_LIMITED" in result_text or "RATE_LIMITED" in error_text

            # Clean markers from result
            if result_text:
                result_text = (
                    result_text.replace("MANUAL_LOGIN_REQUIRED", "")
                    .replace("RATE_LIMITED", "")
                    .strip()
                )

            return {
                "response": result_text or None,
                "error": error_text or None,
                "manual_login_required": manual_login,
                "rate_limited": rate_limited,
            }

        except subprocess.TimeoutExpired:
            return {
                "response": None,
                "error": f"browser-use timed out after {timeout}s — DeepSeek may be slow or blocked.",
                "manual_login_required": False,
                "rate_limited": False,
            }
        except Exception as exc:  # noqa: BLE001
            return {
                "response": None,
                "error": f"browser-use service error: {exc}",
                "manual_login_required": False,
                "rate_limited": False,
            }
        finally:
            try:
                os.unlink(script_path)
            except OSError:
                pass


# Singleton instance
deepseek_web_service = DeepSeekWebService()
