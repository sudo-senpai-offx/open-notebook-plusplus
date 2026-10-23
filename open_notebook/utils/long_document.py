"""Splitting a document so that a bounded context can still see all of it.

Why this exists
---------------
A transformation was handed a source's entire text in one message. The model's
context is smaller than the document, so the provider truncated it - silently -
and the result was presented as if it covered the whole thing.

Measured on a real corpus: the "Dense Summary" of a 212,000-character book
summarised its *title page*. The source opened with the title, the author, the
copyright notice and the publisher's blurb, and the insight was a breakdown of
that blurb. The book was not summarised; its front matter was.

That is the worst kind of failure, because the output looks entirely reasonable.
A student searching a lecture for a term that was in chapter 14 would find
nothing, and nothing in the interface would say why.

So: split the document, run the transformation on each piece, and combine the
pieces. The pieces are summaries, so the combine step is a summary of summaries
- cheap in generation, which is what matters, because on this hardware
generation runs at about 1.1 tokens per second and reading a prompt is nearly
free by comparison.

The chunk size here is deliberately not the embedding chunk size. Those are
different jobs: a 400-token chunk is a vector's worth of text, and a
summarisation chunk wants to be as large as the context allows, because every
extra chunk is another generation call.

Paragraph-aware, and never splits a word in half unless a single paragraph is
larger than the whole budget.
"""

from __future__ import annotations

import os
import re
from typing import List

from loguru import logger

from .token_utils import token_count

#: How much of the context one chunk may occupy, in tokens.
#:
#: The default leaves room for the system prompt, the transformation's own
#: instructions and the model's answer, inside a 4096-token window. It is
#: configurable because the window is: a device that gives the model 16k can
#: afford far fewer chunks, and each chunk is a generation call.
DEFAULT_CHUNK_TOKENS = 3000

#: How much of the previous chunk to repeat at the start of the next one.
#:
#: Without it, a sentence that straddles a boundary is cut in half and the
#: second half is summarised without the first. A twelfth of a chunk is small
#: enough to be cheap and large enough to carry most of a sentence.
DEFAULT_OVERLAP_TOKENS = 250

#: A hard ceiling on how many chunks one document may be split into.
#:
#: Without it, a 305,000-character source becomes a hundred chunks and a
#: hundred generation calls - hours of work, and the job would be killed and
#: retried rather than finished. When the cap bites, the chunks get bigger, so
#: the coverage is still the whole document; it is the resolution that suffers,
#: and the reduction step says so.
DEFAULT_MAX_CHUNKS = 24

#: The smallest chunk worth making.
#:
#: Below this, a chunk carries so little that the per-chunk overhead - the
#: instructions, the model's answer - dominates what it actually says, and the
#: number of generation calls needed to cover the document explodes. It is a
#: floor for a pathological case (a model with a window too small for the
#: work), not a target.
MIN_USEFUL_BUDGET = 256

_SENTENCE_END = re.compile(r"(?<=[.!?。！？])\s+|\n{2,}")


def _chunk_budget() -> int:
    raw = os.getenv("OPEN_NOTEBOOK_TRANSFORMATION_CHUNK_TOKENS")
    if raw:
        try:
            value = int(raw)
            if value > 0:
                return value
        except ValueError:
            pass
    return DEFAULT_CHUNK_TOKENS


def _overlap_budget() -> int:
    raw = os.getenv("OPEN_NOTEBOOK_TRANSFORMATION_CHUNK_OVERLAP_TOKENS")
    if raw:
        try:
            value = int(raw)
            if value >= 0:
                return value
        except ValueError:
            pass
    return DEFAULT_OVERLAP_TOKENS


def _max_chunks() -> int:
    raw = os.getenv("OPEN_NOTEBOOK_TRANSFORMATION_MAX_CHUNKS")
    if raw:
        try:
            value = int(raw)
            if value > 0:
                return value
        except ValueError:
            pass
    return DEFAULT_MAX_CHUNKS


def chunk_budget() -> int:
    """The configured budget, before any model's window is taken into account.

    The configuration is a wish and the model's window is a fact; this is the
    wish, so a caller that has to reconcile the two can say which one it gave
    way.
    """
    return _chunk_budget()


def budget_within_context(context_tokens: int | None, reserved_tokens: int) -> int:
    """The chunk budget that fits inside the model's window.

    The configured budget is a wish; the model's context is a fact. A chunk
    larger than the window is truncated by the provider - silently, and with no
    error - and a truncated chunk is precisely the failure this module exists
    to prevent. So the window wins.

    `reserved_tokens` is what the chunk does not get to use: the instructions
    that precede it, the answer the model writes after it, and a margin for
    the provider's own framing.

    The difference between the wish and the fact is reported by the caller, so
    a device that configured a budget its model cannot honour finds out
    rather than quietly losing text.
    """
    budget = _chunk_budget()
    if not context_tokens or context_tokens <= 0:
        # The model's window is unknown - another provider, or an adapter that
        # does not expose it. The configured budget is then the best guess
        # there is, and budget_message says so on the caller's behalf.
        return budget
    room = context_tokens - reserved_tokens
    if room >= budget:
        return budget
    return max(MIN_USEFUL_BUDGET, room)


def budget_message(
    context_tokens: int | None, budget: int, wanted: int, reserved_tokens: int
) -> str | None:
    """What to report about the budget that was finally used.

    Nothing, when the wish and the fact agree. There are two ways they
    disagree, and both are worth saying out loud:

    the window is known and leaves less room than the wish, so the parts were
    cut down to fit it; or the window is not known at all, in which case the
    wish was accepted unchallenged and is only a guess. The second is the case
    that arrives later as an opaque rejection from the provider, with nothing
    in the log to show that the size was assumed rather than decided - so it is
    reported here rather than being allowed to look like a decision.

    `wanted` is the configured budget and `budget` what was actually used;
    they differ only in the first case.
    """
    if budget < wanted:
        return (
            f"the configured chunk budget of {wanted} tokens does not fit the "
            f"model's window of {context_tokens} tokens once the instructions "
            f"and the answer are allowed for, so the parts will be {budget} "
            f"tokens each; a part larger than the window would be truncated by "
            f"the provider"
        )
    if not context_tokens or context_tokens <= 0:
        return (
            f"the model does not report a context window, so parts are cut to "
            f"the configured {wanted} tokens, and a whole request is "
            f"{wanted + reserved_tokens} tokens with the instructions and the "
            f"answer; a model whose real window is smaller will reject or "
            f"truncate it"
        )
    return None


def _split_long_paragraph(paragraph: str, budget: int) -> List[str]:
    """Break a paragraph that is larger than the whole budget.

    Sentences first, so the break lands between them where it can. Only if a
    single sentence is still too long is it cut at a character boundary - a
    pathological case (a table, a minified blob) that must not swallow the run.
    """
    if token_count(paragraph) <= budget:
        return [paragraph]

    pieces: List[str] = []
    current = ""
    for sentence in _SENTENCE_END.split(paragraph):
        if not sentence:
            continue
        candidate = f"{current} {sentence}".strip()
        if current and token_count(candidate) > budget:
            pieces.append(current)
            current = sentence
        else:
            current = candidate
    if current:
        pieces.append(current)

    out: List[str] = []
    for piece in pieces:
        if token_count(piece) <= budget:
            out.append(piece)
            continue
        # Still too big: cut at a space where possible.
        #
        # The tail is `after_the_space + the_rest_of_the_string`, not just the
        # part after the space. An earlier version kept only the latter and
        # silently dropped everything beyond the window - so the splitter lost
        # text, which is the exact failure this module exists to prevent, and it
        # did it quietly, in a function whose whole job is complete coverage.
        remaining = piece
        while token_count(remaining) > budget:
            approx_chars = max(1, budget * 3)
            window = remaining[:approx_chars]
            head, sep, after = window.rpartition(" ")
            if not sep:
                out.append(window)
                remaining = remaining[approx_chars:]
            else:
                out.append(head)
                remaining = after + remaining[approx_chars:]
        if remaining.strip():
            out.append(remaining)
    return out


def split_for_context(
    text: str,
    budget_tokens: int | None = None,
    overlap_tokens: int | None = None,
    max_chunks: int | None = None,
) -> List[str]:
    """Split `text` into pieces that each fit the context.

    Returns a single-element list when the text already fits, so the caller can
    treat "one chunk" and "several chunks" the same way and only pay for the
    reduction step when there is something to reduce.
    """
    if not text or not text.strip():
        return []

    budget = budget_tokens or _chunk_budget()
    overlap = overlap_tokens if overlap_tokens is not None else _overlap_budget()
    cap = max_chunks or _max_chunks()

    total = token_count(text)
    if total <= budget:
        return [text]

    # The overlap is prepended to the *next* chunk, so it has to come out of
    # that chunk's own budget. Charging it on top meant every chunk ended up
    # larger than the budget it was supposed to fit - which the provider then
    # truncated, throwing away the overlap's purpose along with the tail.
    #
    # And it has to be smaller than the budget: an overlap that leaves no room
    # for content collapses the content budget to almost nothing, and the
    # document can then only be split into more chunks, never fewer. A quarter of
    # the budget is plenty to carry most of a sentence.
    overlap = min(overlap, max(0, budget // 4))
    content_budget = max(64, budget - overlap)

    # The budget is the hard constraint; the cap is advice.
    #
    # They can conflict, and when they do the budget wins. A 12,300-token
    # document cannot be covered by 24 chunks of 300 tokens - that needs 513-token
    # chunks - and an earlier version widened the chunks to satisfy the cap,
    # which pushed them past the budget, which is precisely the truncation this
    # module exists to prevent. The trade is explicit now: the document is
    # covered in full, every chunk fits the context, and the number of
    # generation calls may exceed the cap. The excess is reported, so the cost is
    # visible rather than hidden.
    chunks = _assemble(text, content_budget)
    if len(chunks) > cap:
        logger.warning(
            f"document needs {len(chunks)} parts to fit the context, over the "
            f"advisory cap of {cap}; every part still fits, so the cost is more "
            "calls rather than lost text"
        )

    if overlap > 0 and len(chunks) > 1:
        chunks = _apply_overlap(chunks, overlap)

    return chunks


def _assemble(text: str, content_budget: int) -> List[str]:
    """Split `text` into chunks of at most `content_budget` tokens.

    Each unit is tokenised once and the running total kept as a count, rather
    than re-derived. Re-tokenising the accumulated chunk for every candidate is
    quadratic in the number of units: a 305,000-character source took six
    minutes to split, and the transformation would have sat there before its
    first token of output.
    """
    paragraphs = [p for p in re.split(r"\n{2,}", text) if p.strip()]
    units: List[tuple[str, int]] = []
    for paragraph in paragraphs:
        if token_count(paragraph) > content_budget:
            for piece in _split_long_paragraph(paragraph, content_budget):
                units.append((piece, token_count(piece)))
        else:
            units.append((paragraph, token_count(paragraph)))

    chunks: List[str] = []
    current = ""
    current_tokens = 0
    for unit, unit_tokens in units:
        # The separator costs a couple of tokens, so a little slack is left.
        if current and current_tokens + unit_tokens + 2 > content_budget:
            chunks.append(current)
            current = unit
            current_tokens = unit_tokens
        else:
            current = f"{current}\n\n{unit}" if current else unit
            current_tokens += unit_tokens
    if current:
        chunks.append(current)

    # The accumulated count is an approximation, so the last measurement is the
    # one that counts: a chunk over the budget would be truncated by the
    # provider, which is the failure this module exists to prevent.
    for index, chunk in enumerate(chunks):
        if token_count(chunk) <= content_budget:
            continue
        chunks[index : index + 1] = _split_long_paragraph(chunk, content_budget)
    return chunks


def _apply_overlap(chunks: List[str], overlap: int) -> List[str]:
    """Repeat the tail of each chunk at the head of the next.

    A sentence that straddles a chunk boundary would otherwise be summarised
    with half of it missing, and the two halves can read as contradictory.
    """
    out = [chunks[0]]
    for previous, current in zip(chunks, chunks[1:]):
        # Measured, not assumed. Taking a fixed number of *characters* assumes
        # four characters to a token, which is about right for English and
        # roughly four times too much for Chinese, where a character is very
        # nearly a token. An overlap four times too big pushes the chunk past
        # its budget, and the provider truncates it - which is the failure this
        # whole module exists to prevent, reintroduced through the fix for it.
        tail = _tail_within(previous, overlap)
        out.append(f"{tail}\n\n{current}" if tail.strip() else current)
    return out


def _tail_within(text: str, max_tokens: int) -> str:
    """The tail of `text` that fits in `max_tokens` tokens.

    Found by measuring and trimming, so it is correct for any script. The
    character-count shortcut it replaces assumed four characters per token,
    which is four times too generous for Chinese.
    """
    if max_tokens <= 0 or not text:
        return ""
    # Start wide and narrow down; a token is at least one character.
    window = text[-max_tokens * 8 :] if len(text) > max_tokens * 8 else text
    while window and token_count(window) > max_tokens:
        window = window[1:]
    # Start it at a word boundary so it does not begin mid-word.
    space = window.find(" ")
    if 0 < space < len(window) // 2:
        window = window[space + 1 :]
    return window
