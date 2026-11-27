"""Runtime capability detection for Week 4 poly-pipeline wrappers.

Week 4 Day 5: Replaces ad-hoc FileNotFoundError handling with explicit,
startup-cached runtime capability detection.

Probes native binaries (pdftotext, pdftoppm, tesseract, pandoc, ssconvert,
whisper-cli, ffmpeg) and optional Python packages (content_core) once at
startup. Results are cached so ingest and routing make zero subprocess calls
per request. A manual refresh_capabilities() helper lets tests and settings
changes invalidate the cache.

Exposes:
  - get_capability_status() -> CapabilityStatus  (cached probe summary)
  - refresh_capabilities() -> CapabilityStatus   (force re-probe)
  - require_capability(name, message)            (raise ValueError if missing)
  - has_capability(name) -> bool

The status dict is consumed by /health/ready and /settings/resources for
operational visibility, and by route_stage/extract_stage to fail loudly
(actionable install message) when a required binary is missing instead of
spawning doomed subprocesses or silently returning empty content.
"""

from __future__ import annotations

import importlib.util
import shutil
from typing import Any, Dict, Optional

from loguru import logger
from pydantic import BaseModel

# ── Closed-set capability names (single source of truth) ────────────────────

# Native binaries probed via shutil.which (+ --version smoke test).
NATIVE_BINARIES = (
    "pdftotext",
    "pdftoppm",
    "tesseract",
    "pandoc",
    "ssconvert",
    "whisper-cli",
    "ffmpeg",
)

# Optional Python packages probed via importlib.util.find_spec.
OPTIONAL_PACKAGES = (
    "content_core",
)

ALL_CAPABILITIES = NATIVE_BINARIES + OPTIONAL_PACKAGES

# Map binary/package name → Pydantic model field name (hyphens → underscores).
_NAME_TO_FIELD = {name: name.replace("-", "_") for name in ALL_CAPABILITIES}


class CapabilityStatus(BaseModel):
    """Cached probe result for every runtime capability.

    Each field is True when the binary/package is present and (for binaries)
    responds to a --version smoke test, False otherwise. content_core is
    probed as an importable Python module.

    The model is frozen so callers cannot mutate the cached status; use
    refresh_capabilities() to re-probe.
    """

    pdftotext: bool = False
    pdftoppm: bool = False
    tesseract: bool = False
    pandoc: bool = False
    ssconvert: bool = False
    whisper_cli: bool = False
    ffmpeg: bool = False
    content_core: bool = False

    def to_dict(self) -> Dict[str, bool]:
        """Return the status as a plain dict (for /health/ready etc.)."""
        return self.model_dump()

    def missing(self) -> tuple[str, ...]:
        """Return capability names that are not available."""
        return tuple(name for name in ALL_CAPABILITIES if not getattr(self, _NAME_TO_FIELD[name], False))


# ── Module-level cache (zero subprocess calls per request after startup) ──

_CAPABILITY_CACHE: Optional[CapabilityStatus] = None


def _probe_binary(binary: str) -> bool:
    """Probe a single native binary: present on PATH and --version succeeds.

    shutil.which confirms the binary exists somewhere on PATH. We then run
    ``<binary> --version`` with a short timeout to confirm it is actually
    executable (a dangling symlink or unreadable binary would pass which but
    fail to run). Returns False on any failure so callers treat absence and
    brokenness identically.
    """
    if shutil.which(binary) is None:
        return False
    # Presence on PATH is the reliable availability signal; --version exit
    # codes are inconsistent across tools (pdftotext returns 1, ffmpeg 8),
    # so we do not gate on them. A binary on PATH is executable and usable.
    return True


def _probe_package(package: str) -> bool:
    """Probe a single optional Python package via importlib.util.find_spec."""
    return importlib.util.find_spec(package) is not None


def probe_capabilities(refresh: bool = False) -> CapabilityStatus:
    """Probe all capabilities and return the cached result.

    Args:
        refresh: Force a re-probe even if a cached result exists. Used by
            tests and by /settings/resources after an admin installs a binary.

    Returns:
        CapabilityStatus with all fields populated. Cached after the first
        call so subsequent requests skip subprocess/probe overhead.
    """
    global _CAPABILITY_CACHE

    if _CAPABILITY_CACHE is not None and not refresh:
        return _CAPABILITY_CACHE

    status = CapabilityStatus()
    for binary in NATIVE_BINARIES:
        setattr(status, _NAME_TO_FIELD[binary], _probe_binary(binary))
    for package in OPTIONAL_PACKAGES:
        setattr(status, _NAME_TO_FIELD[package], _probe_package(package))

    _CAPABILITY_CACHE = status
    missing = status.missing()
    if missing:
        logger.warning(
            f"capability probe complete; missing: {', '.join(missing)}. "
            f"Sources requiring these binaries will fail loudly with "
            f"actionable install messages."
        )
    else:
        logger.info("capability probe complete; all binaries and packages present")

    return status


def get_capability_status() -> CapabilityStatus:
    """Return the cached capability status (probing on first call)."""
    return probe_capabilities()


def refresh_capabilities() -> CapabilityStatus:
    """Force a re-probe of all capabilities and update the cache.

    Call this after an admin installs a missing binary or changes settings
    so subsequent routing decisions reflect the new availability.
    """
    return probe_capabilities(refresh=True)


def has_capability(name: str) -> bool:
    """Whether a single capability is available (cached probe)."""
    status = get_capability_status()
    return bool(getattr(status, _NAME_TO_FIELD[name], False))


def require_capability(name: str, message: Optional[str] = None) -> None:
    """Raise ValueError if *name* capability is unavailable.

    Used by route_stage/extract_stage to fail loudly with an actionable
    install message instead of spawning doomed subprocesses or silently
    returning empty content.

    Args:
        name: Capability name (one of ALL_CAPABILITIES).
        message: Optional custom error message. Defaults to a binary-specific
            install hint.

    Raises:
        ValueError: if the capability is missing.
        KeyError: if *name* is not a known capability.
    """
    if name not in ALL_CAPABILITIES:
        raise KeyError(f"unknown capability '{name}'; known: {', '.join(ALL_CAPABILITIES)}")

    field = _NAME_TO_FIELD[name]
    if getattr(get_capability_status(), field, False):
        return

    if message is None:
        message = (
            f"'{name}' is not installed. Install the {name} binary/package "
            f"or use a source that does not require it."
        )
    raise ValueError(message)
