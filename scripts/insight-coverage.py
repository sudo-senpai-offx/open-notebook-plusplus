"""Did an insight cover its source, or only the start of it?

A transformation used to be handed a source's entire text in one message. The
model's context is smaller than the document, the provider truncated it
silently, and the result was presented as covering the whole thing. The
output looked entirely reasonable - which is what makes it the worst kind of
failure - and nothing in the interface said that a chapter fourteen had never
been read.

The document is now split, analysed in parts, and the parts combined. That
fixes the mechanism, but a mechanism that works and a result that covers the
document are two different claims, and only the second one is what a student
actually gets. This checks the second one, on a real source and a real
insight, by asking a question the mechanism cannot fake:

    does the insight talk about things that only appear *after* the point
    where the old path would have stopped?

Usage:

    uv run --env-file .env python scripts/insight-coverage.py "21 Days" "Dense Summary"
    uv run --env-file .env python scripts/insight-coverage.py "21 Days" --all

It prints the vocabulary that only exists in the part of the document the old
path never saw, and how much of it the insight contains. A coverage of zero is
the old failure, and it is worth knowing before a student finds out.
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
from typing import Any, Dict, List

from open_notebook.database.repository import repo_query
from open_notebook.utils.token_utils import token_count


def _words(text: str) -> set[str]:
    """Every word, lowercased. No filter.

    A shape-based filter for "looks like English" was tried and abandoned: it
    rejects real words like "pronunciation" and "curriculum" while accepting
    noise like "aefaveligs". The phrases below need no such filter - an
    insight that quotes the OCR noise is still evidence that it read the
    tail, because the only way to quote it is to have seen it.
    """
    return set(re.findall(r"[A-Za-z]+", text.lower()))


def _trigrams(text: str) -> set[str]:
    """Every run of three consecutive words, lowercased.

    A single word is weak evidence: "accordingly" appears in the middle of
    half the books ever written, so a summary could contain it by accident. A
    run of three words in the same order is not an accident - it is a phrase
    the document used, and the only way for the insight to contain it is for
    the model to have read that part of the document.
    """
    words = re.findall(r"[A-Za-z]+", text.lower())
    return {" ".join(words[i : i + 3]) for i in range(len(words) - 2)}


def _truncation_point(
    document: str, context_tokens: int, instruction_tokens: int
) -> int:
    """Where the old path stopped: the token the provider would have cut at.

    The old code handed the model the whole document in one message and the
    provider kept the beginning of it, up to the model's window less the
    instructions that came with it. Everything after that point is what the
    old insight could not have been about.
    """
    return max(0, context_tokens - instruction_tokens)


def _truncate_to_tokens(document: str, tokens: int) -> str:
    """The first `tokens` tokens of the document, measured rather than guessed."""
    words = document.split()
    if token_count(document) <= tokens:
        return document
    # Binary search on the word boundary: token counts are not linear in
    # characters, so a character count would be wrong for every script that
    # is not English.
    low, high = 0, len(words)
    while low < high:
        mid = (low + high + 1) // 2
        if token_count(" ".join(words[:mid])) <= tokens:
            low = mid
        else:
            high = mid - 1
    return " ".join(words[:low])


async def _fetch(source_title: str) -> Dict[str, Any]:
    rows = (
        await repo_query(
            "SELECT id, title, full_text FROM source WHERE title CONTAINS $t",
            {"t": source_title},
        )
        or []
    )
    if not rows:
        raise SystemExit(f"no source matching {source_title!r}")
    return rows[0]


async def _insights(source_id: str) -> List[Dict[str, Any]]:
    # Fetched and matched here rather than with a `WHERE source = $s`: the
    # column holds a record link, and a bare id does not always match one.
    rows = (
        await repo_query("SELECT source, insight_type, content FROM source_insight")
    ) or []
    return [r for r in rows if str(r.get("source")) == source_id]


async def report(
    source_title: str, transformation_title: str | None, context_tokens: int
) -> int:
    source = await _fetch(source_title)
    document = source.get("full_text") or ""
    insights = await _insights(str(source["id"]))

    if transformation_title:
        insights = [
            i for i in insights if i.get("insight_type") == transformation_title
        ]
        if not insights:
            print(f"  no {transformation_title!r} insight for {source['title']!r}")
            return 1

    if not document.strip():
        print(f"  {source['title']!r} has no extracted text, so there is nothing")
        print("  to cover - the extraction failed before the transformation did")
        return 1

    total = token_count(document)
    # The instructions that came with the document: the template plus the
    # transformation's own prompt. They ate into the window, so the cut was
    # earlier than the window itself.
    instruction_tokens = 700
    seen = _truncation_point(document, context_tokens, instruction_tokens)
    old_view = _truncate_to_tokens(document, seen)

    head_words = _words(old_view)
    tail_words = sorted(_words(document) - head_words)
    head_trigrams = _trigrams(old_view)
    tail_trigrams = _trigrams(document) - head_trigrams

    print(f"  source: {source['title']!r}")
    print(f"  document: {len(document)} characters, {total} tokens")
    print(
        f"  the old path saw the first {seen} of {total} tokens "
        f"({100 * seen / total:.1f}% of the document)"
    )
    print(
        f"  the rest of the document holds {len(tail_trigrams)} three-word "
        f"phrases and {len(tail_words)} distinct words that the old path "
        f"never saw"
    )
    if tail_trigrams:
        sample = "; ".join(repr(p) for p in sorted(tail_trigrams)[:6])
        print(f"    {sample}")

    for insight in insights:
        content = insight.get("content") or ""
        content_trigrams = _trigrams(content)
        phrase_hits = sorted(tail_trigrams & content_trigrams)
        print()
        print(f"  {insight.get('insight_type')}: {len(content)} characters")
        print(
            f"    contains {len(phrase_hits)} of the {len(tail_trigrams)} phrases "
            f"that exist only after the old path's cut"
        )
        if phrase_hits:
            print(f"    {', '.join(repr(p) for p in phrase_hits[:12])}")
        else:
            print(
                "    not one of them - this reads like an insight that only saw "
                "the start of the document"
            )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", help="part of the source's title")
    parser.add_argument(
        "transformation",
        nargs="?",
        help="the insight type to check; every one if omitted",
    )
    parser.add_argument(
        "--context",
        type=int,
        default=8192,
        help="the model's context window in tokens (default: 8192)",
    )
    args = parser.parse_args()
    return asyncio.run(report(args.source, args.transformation, args.context))


if __name__ == "__main__":
    sys.exit(main())
