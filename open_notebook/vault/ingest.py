"""Vault ingest scan (P7). Pure filesystem work: find `.md` files newer
than the watermark. The vault may be a read-only mount, so the watermark
lives in the data dir, never in the vault.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Dict, List, Tuple


def vault_dir() -> str:
    return os.getenv("OPEN_NOTEBOOK_VAULT_DIR", "./vault")


def watermark_path() -> str:
    base = os.getenv("OPEN_NOTEBOOK_DATA_DIR", "./notebook_data")
    return os.path.join(base, "vault_watermark.json")


def scan_vault(
    vault: str, watermark: Dict[str, float]
) -> Tuple[List[Path], Dict[str, float]]:
    """Return (new_or_modified .md files, updated watermark). Missing dir
    yields nothing (vault mount is optional)."""
    root = Path(vault)
    if not root.is_dir():
        return [], dict(watermark)
    found: List[Path] = []
    updated = dict(watermark)
    for path in sorted(root.rglob("*.md")):
        if not path.is_file() or path.is_symlink():
            continue
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        key = str(path.relative_to(root))
        if watermark.get(key) != mtime:
            found.append(path)
        updated[key] = mtime
    return found, updated


def load_watermark(path: str) -> Dict[str, float]:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return {str(k): float(v) for k, v in data.items()}
    except (OSError, ValueError, AttributeError):
        return {}


def save_watermark(path: str, watermark: Dict[str, float]) -> None:
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(watermark, f)
    os.replace(tmp, path)
