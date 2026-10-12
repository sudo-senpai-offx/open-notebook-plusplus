#!/usr/bin/env python3
"""N2a: compare chunk shapes/costs across sizes for a corpus dir.

Usage: python scripts/chunk_compare.py <corpus-dir> [--sizes 256,400,800]
Prints a JSON table per size: chunk counts, avg/max tokens, totals.
Quality (retrieval relevance) needs judged Q/A pairs - see the map.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from open_notebook.utils import chunking
from open_notebook.utils.chunking import chunk_text, describe_chunks


def compare_file(path: str, sizes: list[int]) -> dict:
    with open(path, encoding="utf-8", errors="replace") as f:
        text = f.read()
    row: dict = {"file": os.path.basename(path)}
    for size in sizes:
        # Module global is read at call time (set at import from env).
        chunking.CHUNK_SIZE = size
        chunking.CHUNK_OVERLAP = int(size * 0.15)
        chunks = chunk_text(text)
        stats = describe_chunks(chunks)
        row[str(size)] = stats
    return row


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("corpus_dir")
    parser.add_argument("--sizes", default="256,400,800")
    parser.add_argument("--ext", default=".md")
    args = parser.parse_args()
    sizes = [int(s) for s in args.sizes.split(",")]
    rows = []
    for name in sorted(os.listdir(args.corpus_dir)):
        if name.endswith(args.ext):
            rows.append(compare_file(os.path.join(args.corpus_dir, name), sizes))
    print(json.dumps(rows, indent=2))


if __name__ == "__main__":
    main()
