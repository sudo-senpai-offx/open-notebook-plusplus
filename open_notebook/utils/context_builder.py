"""Context building for chat and podcast generation.

This is the single implementation behind:

- ``POST /api/chat/context`` (`api/routers/chat.py`) — assembles notebook
  context from a source/note inclusion config, via
  :func:`build_notebook_context`.
- the source-chat graph (`open_notebook/graphs/source_chat.py`) — assembles
  a single source plus its insights under a token budget, via
  :func:`build_source_context`.

The inclusion config uses string matching on human-readable status values
("not in context", "insights", "full content"). That protocol is shared with
the frontend — do not change it here.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Optional, Tuple

from loguru import logger

from open_notebook.domain.notebook import (
    Note,
    Notebook,
    Source,
    SourceInsight,
)
from open_notebook.exceptions import DatabaseOperationError, NotFoundError

from .token_utils import token_count

SOURCE_TRUNCATION_NOTICE = (
    "\n\n[Source content truncated to fit the context token budget.]"
)
SOURCE_INSIGHT_BUDGET_RATIO = 0.2
TOKEN_PREFIX_VALIDATION_WINDOW = 16
_TOKENIZER_UNSET = object()


def format_source_context(context_data: Dict[str, Any]) -> str:
    """Render the exact source/insight Markdown supplied to Source Chat."""
    context_parts = []

    if context_data.get("sources"):
        context_parts.append("## SOURCE CONTENT")
        for source in context_data["sources"]:
            if isinstance(source, dict):
                context_parts.append(f"**Source ID:** {source.get('id', 'Unknown')}")
                context_parts.append(f"**Title:** {source.get('title', 'No title')}")
                full_text = source.get("full_text")
                if isinstance(full_text, str) and full_text.strip():
                    context_parts.append(f"**Content:**\n{full_text}")
                else:
                    context_parts.append(
                        "**Content:**\n[Source text is unavailable in this context.]"
                    )
                context_parts.append("")

    if context_data.get("insights"):
        context_parts.append("## SOURCE INSIGHTS")
        for insight in context_data["insights"]:
            if isinstance(insight, dict):
                context_parts.append(f"**Insight ID:** {insight.get('id', 'Unknown')}")
                context_parts.append(
                    f"**Type:** {insight.get('insight_type', 'Unknown')}"
                )
                context_parts.append(
                    f"**Content:** {insight.get('content', 'No content')}"
                )
                context_parts.append("")

    return "\n".join(context_parts)


def _rendered_source_context_tokens(
    source: Optional[Dict[str, Any]],
    insights: list[Dict[str, Any]],
    encoding: Any = _TOKENIZER_UNSET,
) -> int:
    """Count the same Markdown payload that Source Chat sends to its prompt."""
    rendered = format_source_context(
        {
            "sources": [source] if source is not None else [],
            "insights": insights,
        }
    )
    if encoding is _TOKENIZER_UNSET:
        return token_count(rendered)
    if encoding is None:
        return int(len(rendered.split()) * 1.3)
    return len(encoding.encode(rendered, disallowed_special=()))


def _ensure_prefix(table: str, record_id: str) -> str:
    """Ensure a record ID carries its table prefix (`table:id`)."""
    prefix = f"{table}:"
    return record_id if record_id.startswith(prefix) else f"{prefix}{record_id}"


def _truncate_source_to_token_budget(
    source_context: Dict[str, Any],
    max_tokens: int,
    insights: Optional[list[Dict[str, Any]]] = None,
    *,
    encoding: Any = _TOKENIZER_UNSET,
    source_tokens: Optional[list[int]] = None,
    source_is_over_budget: bool = False,
) -> tuple[Optional[Dict[str, Any]], bool]:
    """Truncate source text to a token budget while retaining source metadata.

    A near-maximal token-aligned prefix is retained with bounded validation for
    local BPE non-monotonicity. The truncation notice is part of the budget so
    downstream formatters never need a second size policy.

    Args:
        source_context: Long source context containing ``full_text``.
        max_tokens: Maximum tokens available for rendered source context.
        insights: Already selected insights that share the rendered budget.
        encoding: Reusable tokenizer, ``None`` for the word-count fallback, or
            the internal sentinel when the helper should resolve it itself.
        source_tokens: Precomputed tokens for ``full_text``.
        source_is_over_budget: Skip recounting the full rendered source when
            the caller has already established that it exceeds ``max_tokens``.

    Returns:
        The budgeted source context (or ``None`` when even an explicit notice
        cannot fit) and whether its text was truncated.
    """
    selected_insights = insights or []
    if not source_is_over_budget and (
        _rendered_source_context_tokens(
            source_context,
            selected_insights,
            encoding,
        )
        <= max_tokens
    ):
        return source_context, False

    full_text = source_context.get("full_text")
    if not isinstance(full_text, str) or not full_text.strip():
        return None, False

    def candidate(prefix: str) -> Dict[str, Any]:
        return {
            **source_context,
            "full_text": prefix + SOURCE_TRUNCATION_NOTICE,
        }

    # A very small budget may not even fit source metadata plus the notice.
    # Omit the item; the caller records an explicit status in context metadata.
    notice_only = candidate("")
    if (
        _rendered_source_context_tokens(
            notice_only,
            selected_insights,
            encoding,
        )
        > max_tokens
    ):
        return None, True

    def find_fitting_prefix(
        upper_bound: int,
        prefix_for_count,
        validation_window: int,
    ) -> Optional[Dict[str, Any]]:
        """Find a large fitting prefix with bounded candidate re-encoding."""
        evaluated: Dict[int, tuple[Dict[str, Any], bool]] = {}

        def evaluate(prefix_count: int) -> tuple[Dict[str, Any], bool]:
            if prefix_count not in evaluated:
                prefix = prefix_for_count(prefix_count)
                truncated = candidate(prefix)
                evaluated[prefix_count] = (
                    truncated,
                    bool(prefix.strip())
                    and _rendered_source_context_tokens(
                        truncated,
                        selected_insights,
                        encoding,
                    )
                    <= max_tokens,
                )
            return evaluated[prefix_count]

        low = 1
        high = upper_bound
        best_count = 0
        best: Optional[Dict[str, Any]] = None
        while low <= high:
            midpoint = (low + high) // 2
            truncated, fits = evaluate(midpoint)
            if fits:
                best_count = midpoint
                best = truncated
                low = midpoint + 1
            else:
                high = midpoint - 1

        # BPE counts can be locally non-monotonic. Check a bounded window past
        # the binary-search result without stopping at an over-budget candidate.
        validation_start = best_count + 1 if best_count else 1
        validation_end = min(
            upper_bound,
            best_count + validation_window,
        )
        for prefix_count in range(validation_start, validation_end + 1):
            truncated, fits = evaluate(prefix_count)
            if fits and prefix_count > best_count:
                best_count = prefix_count
                best = truncated

        return best

    try:
        if encoding is _TOKENIZER_UNSET:
            import tiktoken

            encoding = tiktoken.get_encoding("o200k_base")
        if encoding is None:
            raise ImportError
        if source_tokens is None:
            source_tokens = encoding.encode(full_text, disallowed_special=())

        def token_prefix(prefix_token_count: int) -> str:
            return encoding.decode_bytes(source_tokens[:prefix_token_count]).decode(
                "utf-8", errors="ignore"
            )

        best = find_fitting_prefix(
            min(len(source_tokens), max_tokens),
            token_prefix,
            TOKEN_PREFIX_VALIDATION_WINDOW,
        )
        if best is not None:
            return best, True
    except (ImportError, OSError):
        # Match token_count's offline fallback with deterministic word-boundary
        # prefixes. Its split-based estimate is monotonic, so binary search does
        # not need the tokenizer path's validation window.
        word_ends = [match.end() for match in re.finditer(r"\S+", full_text)]

        def word_prefix(word_count: int) -> str:
            return full_text[: word_ends[word_count - 1]]

        best = find_fitting_prefix(
            min(len(word_ends), max_tokens),
            word_prefix,
            0,
        )
        if best is not None:
            return best, True

    # A notice without any source characters is not useful source context.
    # Omit it so callers can report ``omitted_budget`` honestly.
    return None, True


async def build_notebook_context(
    notebook: Notebook,
    context_config: Optional[Dict[str, Any]],
) -> Tuple[Dict[str, list], str]:
    """Assemble source/note context for a notebook.

    With a config, each entry's status string decides inclusion: "not in"
    skips it, "insights" includes the short source context, "full content"
    includes the long context (notes only support "full content"). Without a
    config, every source and note is included with its short context.

    Failures on individual items are logged and skipped — one broken record
    never fails the whole request.

    Returns:
        ({"sources": [...], "notes": [...]}, concatenated str() of every
        included context dict — used for token/char counting).
    """
    context_data: Dict[str, list] = {"sources": [], "notes": []}
    total_content = ""

    if context_config:
        for source_id, status in context_config.get("sources", {}).items():
            if "not in" in status:
                continue

            try:
                full_source_id = _ensure_prefix("source", source_id)

                try:
                    source = await Source.get(full_source_id)
                except Exception:
                    continue

                if "insights" in status:
                    source_context = await source.get_context(context_size="short")
                    context_data["sources"].append(source_context)
                    total_content += str(source_context)
                elif "full content" in status:
                    source_context = await source.get_context(context_size="long")
                    context_data["sources"].append(source_context)
                    total_content += str(source_context)
            except Exception as e:
                logger.warning(f"Error processing source {source_id}: {str(e)}")
                continue

        for note_id, status in context_config.get("notes", {}).items():
            if "not in" in status:
                continue

            try:
                full_note_id = _ensure_prefix("note", note_id)
                note = await Note.get(full_note_id)
                if not note:
                    continue

                if "full content" in status:
                    note_context = note.get_context(context_size="long")
                    context_data["notes"].append(note_context)
                    total_content += str(note_context)
            except Exception as e:
                logger.warning(f"Error processing note {note_id}: {str(e)}")
                continue
    else:
        # Default behavior - include all sources and notes with short context
        sources = await notebook.get_sources()
        try:
            insights_by_source = await SourceInsight.get_for_sources(
                [source.id for source in sources if source.id]
            )
        except Exception as e:
            # Match the per-source fallback below: a hiccup fetching
            # insights shouldn't fail the whole context request.
            logger.warning(f"Error batch-fetching source insights: {str(e)}")
            insights_by_source = {}
        for source in sources:
            try:
                source_context = await source.get_context(
                    context_size="short",
                    insights=insights_by_source.get(source.id or "", []),
                )
                context_data["sources"].append(source_context)
                total_content += str(source_context)
            except Exception as e:
                logger.warning(f"Error processing source {source.id}: {str(e)}")
                continue

        notes = await notebook.get_notes()
        for note in notes:
            try:
                note_context = note.get_context(context_size="short")
                context_data["notes"].append(note_context)
                total_content += str(note_context)
            except Exception as e:
                logger.warning(f"Error processing note {note.id}: {str(e)}")
                continue

    return context_data, total_content


async def build_source_context(
    source_id: str, max_tokens: Optional[int] = None
) -> Dict[str, Any]:
    """Assemble a single source's full text plus its insights.

    Used by the source-chat graph. If ``max_tokens`` is given, the source text
    is kept in full when it fits, then insights are retained in fetch order
    while space remains. When the source alone exceeds the budget, a bounded
    share is reserved for insights and the source is explicitly truncated into
    the remaining space instead of being dropped.

    Returns a dict with "sources", "notes" (always empty), "insights",
    "total_tokens", "total_items" and per-type counts in "metadata".
    """
    try:
        sources: list = []
        insights: list = []
        source_truncated = False
        source_text_status = "not_found"

        try:
            full_source_id = _ensure_prefix("source", source_id)
            source = await Source.get(full_source_id)
        except NotFoundError:
            source = None

        if source:
            insight_objects = await source.get_insights()
            source_context = await source.get_context(
                context_size="long",
                insights=insight_objects,
            )
            # Insights have their own budgeted items below. Keeping the nested
            # copy would double-count them while the formatter ignores it.
            source_context = {**source_context, "insights": []}
            budgeted_source: Optional[Dict[str, Any]] = source_context

            insight_items: list[Dict[str, Any]] = []
            for insight in insight_objects:
                insight_content = {
                    "id": insight.id,
                    "source_id": source.id,
                    "insight_type": insight.insight_type,
                    "content": insight.content,
                }
                insight_items.append(insight_content)

            try:
                import tiktoken

                encoding = tiktoken.get_encoding("o200k_base")
            except (ImportError, OSError):
                encoding = None

            rendered_source_tokens = _rendered_source_context_tokens(
                source_context,
                [],
                encoding,
            )

            if max_tokens is None:
                insights.extend(insight_items)
            elif rendered_source_tokens > max_tokens:
                # Large documents would otherwise consume the entire budget and
                # silently remove every insight. Reserve a bounded share for
                # insights, then give all unused space back to the source text.
                insight_budget = int(max_tokens * SOURCE_INSIGHT_BUDGET_RATIO)
                for insight_content in insight_items:
                    candidate_insights = [*insights, insight_content]
                    if (
                        _rendered_source_context_tokens(
                            None,
                            candidate_insights,
                            encoding,
                        )
                        > insight_budget
                    ):
                        continue
                    insights.append(insight_content)

                full_text = source_context.get("full_text")
                encoded_source = (
                    encoding.encode(full_text, disallowed_special=())
                    if encoding is not None and isinstance(full_text, str)
                    else None
                )

                budgeted_source, source_truncated = _truncate_source_to_token_budget(
                    source_context,
                    max_tokens,
                    insights,
                    encoding=encoding,
                    source_tokens=encoded_source,
                    source_is_over_budget=True,
                )
            else:
                for insight_content in insight_items:
                    candidate_insights = [*insights, insight_content]
                    if (
                        _rendered_source_context_tokens(
                            source_context,
                            candidate_insights,
                            encoding,
                        )
                        > max_tokens
                    ):
                        continue
                    insights.append(insight_content)

            if budgeted_source is None:
                source_text_status = "omitted_budget"
            else:
                full_text = budgeted_source.get("full_text")
                if isinstance(full_text, str) and full_text.strip():
                    source_text_status = (
                        "truncated" if source_truncated else "available"
                    )
                else:
                    source_text_status = "missing"

            if budgeted_source is not None:
                sources.append(budgeted_source)
            total_tokens = _rendered_source_context_tokens(
                budgeted_source,
                insights,
                encoding,
            )
        else:
            logger.warning(f"Source {source_id} not found")
            total_tokens = 0

        total_items = len(sources) + len(insights)
        logger.info(f"Built context with {total_items} items, {total_tokens} tokens")

        return {
            "sources": sources,
            "notes": [],
            "insights": insights,
            "total_tokens": total_tokens,
            "total_items": total_items,
            "metadata": {
                "source_count": len(sources),
                "note_count": 0,
                "insight_count": len(insights),
                "source_text_status": source_text_status,
                "source_truncated": source_truncated,
            },
        }
    except Exception as e:
        logger.error(f"Error building context: {str(e)}")
        raise DatabaseOperationError(f"Failed to build context: {str(e)}")


# How many chunks retrieval keeps, and how lax the similarity bar is.
#
# 8 chunks is roughly what fits alongside a question in a 4k window once
# the system prompt and the answer are accounted for. The point is not to
# maximise recall - it is to get the relevant passages *into the window at
# all*, which the whole-document dump below does not manage.
RETRIEVAL_LIMIT = 8
# 0.45, not the 0.2 the search endpoint defaults to. Measured on this
# library with bge-m3: genuine topical matches land at 0.52-0.61, and a
# question with nothing to do with the corpus - "how do I bake sourdough
# bread" - still returned electrophoresis notes at 0.44. At 0.2 that noise
# is indistinguishable from a real hit, and a small model will happily
# answer from it. Above this bar, a nonsense question retrieves nothing,
# which makes the caller fall back to whole documents rather than answer
# confidently from an unrelated slide deck.
RETRIEVAL_MINIMUM_SCORE = 0.45

# Hard ceiling on retrieved text. The phone runs a 4k-token window that
# also has to hold the system prompt, the question and the answer; at
# roughly four characters per token this leaves room for all three. The
# whole-document dump this replaces had no ceiling at all, which is how a
# 40,000-character prompt ended up in a 16,384-character window.
MAX_CONTEXT_CHARACTERS = 6000


async def build_relevant_context(
    notebook_id: Optional[str],
    query: str,
    eligible_ids: Optional[Dict[str, set]] = None,
    limit: int = RETRIEVAL_LIMIT,
    minimum_score: float = RETRIEVAL_MINIMUM_SCORE,
    max_characters: int = MAX_CONTEXT_CHARACTERS,
) -> Tuple[Dict[str, list], str, Dict[str, Any]]:
    """Assemble chat context from the passages that match the question.

    Why this exists
    ----------------
    :func:`build_notebook_context` has no notion of the question. It walks
    a user-made inclusion list and pastes whole sources and notes into the
    prompt, so the model receives tens of thousands of characters and is
    expected to select the relevant part itself. On a phone-sized window
    most of that is discarded before the model reads it, and a small model
    is not going to do the selecting well.

    Retrieval moves that work out of the model and into the embedding
    index, which is already built and already queried by
    ``POST /api/search``. A 1.5B model given eight relevant chunks is a
    different proposition from the same model given a whole notebook.

    Scope still comes from the user: *eligible_ids* carries the ids their
    inclusion config selected, and a passage from anything else is
    discarded. Relevance chooses the content; the checkbox chooses the
    bookshelf.

    Every failure degrades to the caller's existing behaviour rather than
    raising. A device with no embedding model configured, an index that
    has not been built yet, or an embedding dimension that does not match
    are all ordinary states, not errors - and the fallback is the context
    the user would have got anyway.
    """
    from open_notebook.domain.notebook import vector_search

    # Every key is present from the start. A report whose shape changes
    # depending on the outcome is a KeyError waiting for whoever reads it
    # next, and the caller cannot tell an early return from a finished
    # search without checking which keys came back.
    report: Dict[str, Any] = {
        "mode": "retrieval",
        "chunks": 0,
        "documents": 0,
        "characters": 0,
        "dropped_out_of_scope": 0,
        "dropped_over_budget": 0,
    }

    try:
        rows = await vector_search(
            keyword=query,
            results=limit,
            source=True,
            note=True,
            minimum_score=minimum_score,
            # None means search everything. The endpoint always scopes to a
            # notebook; this exists because a device whose sources have not
            # been grouped into one yet would otherwise retrieve nothing at
            # all, which looks identical to "the index is empty".
            notebook_ids=[notebook_id] if notebook_id else None,
        )
    except Exception as exc:  # noqa: BLE001 - degrade, never fail a chat turn
        logger.info(f"Context retrieval unavailable, using whole documents: {exc}")
        report["mode"] = "fallback"
        report["reason"] = str(exc)[:160]
        raise

    if not rows:
        # Nothing scored above the bar. An empty prompt is worse than the
        # document dump the caller would otherwise send.
        logger.info("Retrieval matched nothing; caller should fall back")
        report["mode"] = "empty"
        return {"sources": [], "notes": []}, "", report

    # `fn::vector_search` already does the grouping. Its final RETURN is
    # `array::flatten(content) as matches` over `group by id, parent_id,
    # title` with `math::max(similarity)`, so a row is one *document*
    # carrying its matching passages - not one passage.
    #
    # Reading `row["content"]` instead returns nothing at all, and looks
    # exactly like an empty index.
    context_data: Dict[str, list] = {"sources": [], "notes": []}
    kept: list[Dict[str, Any]] = []

    # Sorted here rather than trusted to arrive that way. `vector_search`
    # does sort, but a budget that silently drops the best matches
    # because an ordering contract changed upstream is not a failure
    # anyone would notice.
    for row in sorted(
        rows or [], key=lambda item: -float(item.get("similarity") or 0.0)
    ):
        record_id = str(row.get("parent_id") or row.get("id") or "")
        if not record_id:
            continue
        if eligible_ids is not None:
            kind = "notes" if record_id.startswith("note:") else "sources"
            allowed = eligible_ids.get(kind)
            if allowed is not None and record_id not in allowed:
                report["dropped_out_of_scope"] += 1
                continue

        # `matches` is an array of the document's matching passages.
        matches = row.get("matches") or []
        if isinstance(matches, str):
            matches = [matches]
        passages = [str(m).strip() for m in matches if str(m).strip()]
        if not passages:
            continue

        kept.append(
            {
                "id": record_id,
                "title": row.get("title") or "",
                "relevant_content": "\n\n[... ]\n\n".join(passages),
                "similarity": round(float(row.get("similarity") or 0.0), 4),
            }
        )

    # `limit` bounds documents, and each document can carry several
    # passages, so the character budget is enforced separately. Without it
    # eight generous sources can still overflow a 4k window - which is the
    # failure this whole function exists to prevent.
    budget = max_characters
    for payload in kept:
        if budget <= 0:
            report["dropped_over_budget"] += 1
            continue
        if len(payload["relevant_content"]) > budget:
            payload["relevant_content"] = payload["relevant_content"][:budget]
        budget -= len(payload["relevant_content"])
        context_data[
            "notes" if payload["id"].startswith("note:") else "sources"
        ].append(payload)
        report["chunks"] += payload["relevant_content"].count("[... ]") + 1

    report["documents"] = len(context_data["sources"]) + len(context_data["notes"])
    report["characters"] = sum(
        len(e["relevant_content"])
        for e in context_data["sources"] + context_data["notes"]
    )
    return context_data, "", report
