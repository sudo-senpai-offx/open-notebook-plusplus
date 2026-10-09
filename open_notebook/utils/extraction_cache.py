"""Simple extraction cache keyed by content hash."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Optional


def _cache_dir() -> Path:
    base = (
        Path(os.getenv("OPEN_NOTEBOOK_DATA_DIR", "./notebook_data"))
        / "cache"
        / "extraction"
    )
    base.mkdir(parents=True, exist_ok=True)
    return base


def _key_for(
    file_path: Optional[str], url: Optional[str], content: Optional[str], config: Any
) -> str:
    h = hashlib.sha256()
    if file_path and os.path.exists(file_path):
        try:
            with open(file_path, "rb") as f:
                h.update(f.read(8 * 1024 * 1024))  # up to 8MB
        except Exception:
            pass
    if url:
        h.update(url.encode("utf-8"))
    if content:
        h.update(content.encode("utf-8"))
    if config is not None:
        try:
            h.update(
                str(getattr(config, "model_dump", lambda: str(config))()).encode(
                    "utf-8"
                )
            )
        except Exception:
            try:
                h.update(str(config).encode("utf-8"))
            except Exception:
                pass
    return h.hexdigest()


def get_cached(
    file_path: Optional[str] = None,
    url: Optional[str] = None,
    content: Optional[str] = None,
    config: Any = None,
) -> Optional[dict[str, Any]]:
    try:
        key = _key_for(file_path, url, content, config)
        p = _cache_dir() / f"{key}.json"
        if p.exists():
            return json.loads(p.read_text())
    except Exception:
        pass
    return None


def set_cached(
    result: dict[str, Any],
    file_path: Optional[str] = None,
    url: Optional[str] = None,
    content: Optional[str] = None,
    config: Any = None,
) -> None:
    try:
        key = _key_for(file_path, url, content, config)
        p = _cache_dir() / f"{key}.json"
        p.write_text(json.dumps(result, indent=2, default=str))
    except Exception:
        pass
