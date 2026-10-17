"""Instruction prefixes for embedding models that require one.

Not all embedding models want the same input. Some are trained on bare text;
others are trained on text with an instruction that tells the model *what role*
the text is playing, and retrieving badly without it:

- **E5** (`intfloat/e5-*`, `multilingual-e5-*`) expects `"query: "` and
  `"passage: "`. This is not a formatting nicety - it is how the model was
  trained, and leaving it off measurably degrades retrieval.
- **Nomic** expects `"search_query: "` and `"search_document: "`.
- **BGE** (`bge-*`, including `bge-m3`) expects **neither**. Prefixing BGE
  text is not harmless; it pushes vectors off the distribution they were
  trained on.

That last point is why this is keyed on the model name rather than applied
unconditionally. The PC runs `bge-m3` and the phone was moved to a
multilingual model; a global prefix would have quietly degraded the side that
was already working.

A wrong prefix is worse than none, so detection is deliberately conservative:
a model we do not recognise gets no prefix, which is the behaviour every
model had before this existed.
"""

from __future__ import annotations

from enum import Enum


class EmbeddingRole(str, Enum):
    """What a piece of text is *for*.

    The same embedding function serves both sides of a search, so the role
    cannot be inferred from the call - it has to be passed.
    """

    QUERY = "query"
    PASSAGE = "passage"


#: (query prefix, passage prefix) per model family, matched case-insensitively
#: against the configured model name. An empty string means "no prefix".
_FAMILY_PREFIXES: tuple[tuple[str, str, str], ...] = (
    # E5 - the whole family, including the multilingual variants. Matched on
    # "e5" rather than the full org path because the phone registers the model
    # by GGUF file path, which contains no repository name at all.
    ("e5", "query: ", "passage: "),
    ("nomic", "search_query: ", "search_document: "),
)

#: Families that must never be prefixed. Checked before the table above so a
#: future naming collision cannot prefix a BGE model.
_NEVER_PREFIX = ("bge",)


def requires_prefix(model_name: str) -> bool:
    """Whether *model_name* is a family we know wants an instruction."""
    lowered = (model_name or "").lower()
    if any(marker in lowered for marker in _NEVER_PREFIX):
        return False
    return any(marker in lowered for marker, _, _ in _FAMILY_PREFIXES)


def prefix_for(model_name: str, role: EmbeddingRole) -> str:
    """The instruction this model expects for *role*, or "" if it expects none."""
    lowered = (model_name or "").lower()
    if any(marker in lowered for marker in _NEVER_PREFIX):
        return ""
    for marker, query_prefix, passage_prefix in _FAMILY_PREFIXES:
        if marker in lowered:
            return query_prefix if role is EmbeddingRole.QUERY else passage_prefix
    return ""


def apply_prefix(text: str, model_name: str, role: EmbeddingRole) -> str:
    """*text* with the model's instruction for *role* in front.

    Idempotent: a text that already carries the prefix is left alone. Chunking
    can call this more than once for the same string, and a doubled
    `"query: query: "` is both wasteful and a different input distribution
    from the one the model was trained on.
    """
    prefix = prefix_for(model_name, role)
    if not prefix or not text:
        return text
    if text.startswith(prefix):
        return text
    return f"{prefix}{text}"


__all__ = [
    "EmbeddingRole",
    "apply_prefix",
    "prefix_for",
    "requires_prefix",
]
