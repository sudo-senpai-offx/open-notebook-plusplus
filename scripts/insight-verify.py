"""Run the current transformation on a source, and report what each part gave.

The map-reduce has to be checked on the *requests*, not on the arithmetic that
produces them: a part can be larger than the model's window, or sent twice, or
come back empty, and the answer still looks reasonable because the model coped.

This runs the real transformation - the real prompts, the real model - and
reports the size of every part, so a part that is empty or suspiciously large
is visible rather than inferred from the final answer.

    python scripts/insight-verify.py "Eukaryote" "Dense Summary" --chunk-tokens 1200

The chunk budget is the size of one part. Lowering it makes a short document
split into several parts, which is the cheapest way to exercise the whole
path: a document that needs one part never reaches the map step at all.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from typing import Any

from open_notebook.database.repository import repo_query


class _Transformation:
    """The fields the transformation reads."""

    def __init__(self, prompt: str, title: str):
        self.prompt = prompt
        self.title = title


async def run(
    source_title: str, transformation_title: str, chunk_tokens: int | None
) -> int:
    if chunk_tokens:
        os.environ["OPEN_NOTEBOOK_TRANSFORMATION_CHUNK_TOKENS"] = str(chunk_tokens)

    from open_notebook.graphs.transformation import run_transformation

    sources = (
        await repo_query(
            "SELECT id, title, full_text FROM source WHERE title CONTAINS $t",
            {"t": source_title},
        )
        or []
    )
    if not sources:
        print(f"no source matching {source_title!r}")
        return 1
    source = sources[0]

    transformations = (
        await repo_query(
            "SELECT prompt, title FROM transformation WHERE title = $t",
            {"t": transformation_title},
        )
        or []
    )
    if not transformations:
        print(f"no transformation titled {transformation_title!r}")
        return 1
    transformation = _Transformation(
        transformations[0]["prompt"], transformations[0]["title"]
    )

    document = source.get("full_text") or ""
    from open_notebook.utils.token_utils import token_count

    print(f"  source: {source['title']!r}")
    print(f"  document: {len(document)} characters, {token_count(document)} tokens")
    if chunk_tokens:
        print(f"  one part may be {chunk_tokens} tokens")

    state: dict[str, Any] = {
        "source": None,
        "input_text": document,
        "transformation": transformation,
    }
    result = await run_transformation(state, config={"configurable": {}})
    answer = result["output"]
    print(f"  the answer: {len(answer)} characters")
    print()
    print(answer)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", help="part of the source's title")
    parser.add_argument("transformation", help="the transformation's title")
    parser.add_argument(
        "--chunk-tokens",
        type=int,
        help="the size of one part, in tokens",
    )
    args = parser.parse_args()
    return asyncio.run(run(args.source, args.transformation, args.chunk_tokens))


if __name__ == "__main__":
    sys.exit(main())
