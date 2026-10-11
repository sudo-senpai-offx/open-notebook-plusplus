"""Isolated tiny-training entry point (T5-a2).

Reads a JSON input file `{text, max_steps, max_seconds}`, runs the gated
core, prints the result dict as JSON on stdout. Any failure exits nonzero
with a traceback on stderr — the parent surfaces it, never swallows it.

Usage: python -m open_notebook.training.tiny_worker <input.json>
"""

from __future__ import annotations

import json
import sys


def main(argv: list[str]) -> int:
    with open(argv[1], encoding="utf-8") as f:
        payload = json.load(f)
    from open_notebook.training.tiny import run_tiny_train

    result = run_tiny_train(
        payload["text"],
        max_steps=int(payload.get("max_steps", 200)),
        max_seconds=int(payload.get("max_seconds", 300)),
    )
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
