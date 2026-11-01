"""Clipboard sync service via linuxdrop subprocess.

Text + image clipboard only (no files — those go through the upload feature).
Runs linuxdrop as a direct subprocess, reads the latest clipboard entry,
and returns it to the caller for injection into chat input.

Not a persistent service: spawned on demand, read once, exited.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from typing import Any, Dict, Optional

# Default locations where a linuxdrop client binary might live.
LINUXDROP_CANDIDATES = [
    os.path.expanduser("~/bin/linuxdrop"),
    os.path.expanduser("~/.local/bin/linuxdrop"),
    "/usr/local/bin/linuxdrop",
    "/usr/bin/linuxdrop",
]


class ClipboardService:
    """Direct subprocess access to linuxdrop clipboard sync.

    Methods are fail-safe: if linuxdrop isn't installed or the clipboard
    is empty, returns empty results rather than raising.
    """

    def __init__(self, binary: Optional[str] = None) -> None:
        self.binary = binary or self._find_binary()

    def _find_binary(self) -> Optional[str]:
        """Locate the linuxdrop client binary."""
        for path in LINUXDROP_CANDIDATES:
            if os.path.isfile(path) and os.access(path, os.X_OK):
                return path
        # Fall back to PATH lookup
        found = shutil.which("linuxdrop")
        if found:
            return found
        return None

    def is_available(self) -> bool:
        """True if a linuxdrop client binary is present."""
        return self.binary is not None

    def get_latest(self) -> Dict[str, Any]:
        """Read the latest clipboard entry from linuxdrop.

        Returns a dict with keys:
          - `text`: str or None
          - `image`: bool (whether an image is available)
          - `available`: bool
          - `error`: str or None
        """
        if not self.binary:
            return {
                "text": None,
                "image": False,
                "available": False,
                "error": "linuxdrop binary not found",
            }

        try:
            # linuxdrop CLI: `linuxdrop clipboard get` returns JSON
            result = subprocess.run(
                [self.binary, "clipboard", "get", "--format", "json"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode != 0:
                return {
                    "text": None,
                    "image": False,
                    "available": False,
                    "error": f"linuxdrop exited {result.returncode}: {result.stderr.strip()[:200]}",
                }

            import json

            data = json.loads(result.stdout or "{}")
            return {
                "text": data.get("text"),
                "image": bool(data.get("image", False)),
                "available": bool(data.get("text") or data.get("image")),
                "error": None,
            }
        except subprocess.TimeoutExpired:
            return {
                "text": None,
                "image": False,
                "available": False,
                "error": "linuxdrop timed out",
            }
        except Exception as exc:  # noqa: BLE001
            return {
                "text": None,
                "image": False,
                "available": False,
                "error": f"linuxdrop error: {exc}",
            }

    def set_text(self, text: str) -> Dict[str, Any]:
        """Write text to the clipboard via linuxdrop."""
        if not self.binary:
            return {"ok": False, "error": "linuxdrop binary not found"}
        try:
            result = subprocess.run(
                [self.binary, "clipboard", "set", "--text", text],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode != 0:
                return {
                    "ok": False,
                    "error": f"linuxdrop exited {result.returncode}: {result.stderr.strip()[:200]}",
                }
            return {"ok": True}
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": "linuxdrop timed out"}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"linuxdrop error: {exc}"}


# Singleton instance
clipboard_service = ClipboardService()
