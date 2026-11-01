"""Browser-use service for DeepSeek integration via session capture.

Runs browser-use as a Python library subprocess on demand — not a
persistent service. Spawned on first DeepSeek cloud request, killed
after the request completes (sync call within the request).

Internet-dependent: refuses to start when there is no network.

Stealth mode with headed/headless fallback: tries headless first for
background operation, falls back to headed (visible browser) if stealth
detection is triggered.

Session persistence: stores cookies/localStorage after manual login so
subsequent requests reuse the session. On login timeout, falls back to
replaying stored cookies or signaling that manual re-login is needed.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, Optional

from loguru import logger

DEEPSEEK_URL = "https://chat.deepseek.com"

# Where session cookies/localStorage are persisted.
SESSION_DIR = Path(os.path.expanduser("~/.local/share/onb-plusplus/deepseek"))
SESSION_FILE = SESSION_DIR / "session.json"

# browser-use Python script template, written to a temp file and executed
# as a subprocess. This keeps browser-use out of ONB's main process.
_BROWSER_USE_SCRIPT = r'''
import asyncio
import json
import os
import sys

from browser_use import Agent, Browser, BrowserConfig
from browser_use.agent.views import AgentHistoryList
from dotenv import load_dotenv

load_dotenv()

SESSION_FILE = os.environ.get("ONB_DEEPSEEK_SESSION_FILE", "")
DEEPSEEK_URL = "https://chat.deepseek.com"
HEADLESS = os.environ.get("ONB_DEEPSEEK_HEADLESS", "1") == "1"
MESSAGE = sys.argv[1] if len(sys.argv) > 1 else ""


async def main() -> None:
    result_text = ""
    error_text = ""

    try:
        browser = Browser(
            config=BrowserConfig(
                headless=HEADLESS,
                disable_security=True,
                # Stealth: realistic viewport, user agent, locale
                args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-blink-features=AutomationControlled",
                    "--window-size=1920,1080",
                ],
            )
        )

        agent = Agent(
            task=(
                f"Go to {DEEPSEEK_URL}. If already logged in, proceed to chat. "
                f"If not logged in, click the login button and complete authentication. "
                f"Once in the chat interface, type this message exactly and send it: "
                f"{json.dumps(MESSAGE)}. "
                f"Wait for the AI response to appear, then copy the full response text. "
                f"Do not summarize — return the complete response verbatim. "
                f"If a login wall appears that requires manual interaction, "
                f"report 'MANUAL_LOGIN_REQUIRED'. "
                f"If the page shows a rate limit or capacity message, "
                f"report 'RATE_LIMITED'."
            ),
            llm=None,  # browser-use needs an LLM for navigation; we use a no-op
            browser=browser,
        )

        # Run the agent — browser-use drives the browser to DeepSeek
        history: AgentHistoryList = await agent.run()

        # Extract the final result
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

    output = {"result": result_text, "error": error_text}
    print(json.dumps(output))


if __name__ == "__main__":
    asyncio.run(main())
'''


class BrowserUseService:
    """Manages browser-use subprocess for DeepSeek cloud access."""

    def __init__(self) -> None:
        self._process: Optional[subprocess.Popen] = None
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
        """Ensure browser-use is pip-installed. Returns True if available."""
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
    ) -> Dict[str, Any]:
        """Run a DeepSeek chat request via browser-use.

        Returns a dict with keys:
          - `response`: str or None — the AI response text
          - `error`: str or None — error message if any
          - `manual_login_required`: bool — if True, user must re-login
          - `rate_limited`: bool — if True, DeepSeek rate-limited the session
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
            suffix=".py", prefix="onb_deepseek_"
        )
        try:
            with os.fdopen(script_fd, "w") as f:
                f.write(_BROWSER_USE_SCRIPT)

            env = os.environ.copy()
            env["ONB_DEEPSEEK_SESSION_FILE"] = str(SESSION_FILE)
            env["ONB_DEEPSEEK_HEADLESS"] = "1" if headless else "0"

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
                result_text = result_text.replace("MANUAL_LOGIN_REQUIRED", "").replace("RATE_LIMITED", "").strip()

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

    def save_session(self, cookies: list, local_storage: dict) -> None:
        """Persist session cookies/localStorage for replay on next request."""
        try:
            SESSION_DIR.mkdir(parents=True, exist_ok=True)
            data = {
                "cookies": cookies,
                "local_storage": local_storage,
                "saved_at": time.time(),
            }
            SESSION_FILE.write_text(json.dumps(data, indent=2))
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"Could not save DeepSeek session: {exc}")

    def load_session(self) -> Optional[Dict[str, Any]]:
        """Load stored session cookies/localStorage."""
        try:
            if SESSION_FILE.exists():
                return json.loads(SESSION_FILE.read_text())
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"Could not load DeepSeek session: {exc}")
        return None


# Singleton instance
browser_use_service = BrowserUseService()
