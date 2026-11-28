"""
Chunking utilities for Open Notebook.

Provides content-type detection and smart text chunking for embedding operations.
Supports HTML, Markdown, and plain text with appropriate splitters for each type.

Key functions:
- detect_content_type(): Detects content type from file extension or content heuristics
- chunk_text(): Splits text into chunks using appropriate splitter for content type

Environment Variables:
    OPEN_NOTEBOOK_CHUNK_SIZE: Maximum chunk size in tokens (default: 400)
    OPEN_NOTEBOOK_CHUNK_OVERLAP: Overlap between chunks in tokens (default: 15% of CHUNK_SIZE)
    OPEN_NOTEBOOK_MIN_CHUNK_SIZE: Minimum chunk size in tokens (default: 5)
"""

import os
import re
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from langchain_text_splitters import (
    HTMLHeaderTextSplitter,
    MarkdownHeaderTextSplitter,
    RecursiveCharacterTextSplitter,
)
from loguru import logger

from .token_utils import token_count


def _get_chunk_size() -> int:
    """Get chunk size from environment variable or use default."""
    chunk_size_str = os.getenv("OPEN_NOTEBOOK_CHUNK_SIZE")
    if chunk_size_str:
        try:
            chunk_size = int(chunk_size_str)
            if chunk_size < 100:
                logger.warning(
                    f"OPEN_NOTEBOOK_CHUNK_SIZE ({chunk_size}) is too small. "
                    f"Using minimum value of 100."
                )
                return 100
            if chunk_size > 8192:
                logger.warning(
                    f"OPEN_NOTEBOOK_CHUNK_SIZE ({chunk_size}) is very large. "
                    f"This may cause issues with some embedding models."
                )
            logger.info(f"Using custom chunk size: {chunk_size} tokens")
            return chunk_size
        except ValueError:
            logger.warning(
                f"Invalid OPEN_NOTEBOOK_CHUNK_SIZE value: '{chunk_size_str}'. "
                f"Using default: 400"
            )
    return 400


def _get_chunk_overlap(chunk_size: int) -> int:
    """Get chunk overlap from environment variable or calculate default (15% of chunk size)."""
    overlap_str = os.getenv("OPEN_NOTEBOOK_CHUNK_OVERLAP")
    if overlap_str:
        try:
            overlap = int(overlap_str)
            if overlap < 0:
                logger.warning(
                    f"OPEN_NOTEBOOK_CHUNK_OVERLAP ({overlap}) cannot be negative. "
                    f"Using 0."
                )
                return 0
            if overlap >= chunk_size:
                logger.warning(
                    f"OPEN_NOTEBOOK_CHUNK_OVERLAP ({overlap}) cannot be >= chunk size ({chunk_size}). "
                    f"Using 15% of chunk size: {int(chunk_size * 0.15)}"
                )
                return int(chunk_size * 0.15)
            logger.info(f"Using custom chunk overlap: {overlap} tokens")
            return overlap
        except ValueError:
            logger.warning(
                f"Invalid OPEN_NOTEBOOK_CHUNK_OVERLAP value: '{overlap_str}'. "
                f"Using default: 15% of chunk size"
            )
    return int(chunk_size * 0.15)


def _get_min_chunk_size() -> int:
    """Get minimum chunk size from environment variable or use default.

    Chunks below this token count are dropped. Some splitters (notably the
    HTML header splitter on complex pages) can emit single-character or
    punctuation-only chunks that produce useless or null embeddings —
    llama.cpp's OpenAI-compatible endpoint, for example, returns null vector
    elements for such inputs and crashes downstream parsing.
    """
    raw = os.getenv("OPEN_NOTEBOOK_MIN_CHUNK_SIZE")
    if raw is None:
        return 5
    try:
        value = int(raw)
        if value < 0:
            logger.warning(
                f"OPEN_NOTEBOOK_MIN_CHUNK_SIZE ({value}) cannot be negative. Using 0."
            )
            return 0
        return value
    except ValueError:
        logger.warning(
            f"Invalid OPEN_NOTEBOOK_MIN_CHUNK_SIZE value: '{raw}'. Using default: 5"
        )
        return 5


# Constants (computed at import time from environment variables)
CHUNK_SIZE = _get_chunk_size()
CHUNK_OVERLAP = _get_chunk_overlap(CHUNK_SIZE)
MIN_CHUNK_SIZE = _get_min_chunk_size()
HIGH_CONFIDENCE_THRESHOLD = 0.8  # Threshold for heuristics to override extension

logger.debug(
    f"Chunking configuration: CHUNK_SIZE={CHUNK_SIZE}, "
    f"CHUNK_OVERLAP={CHUNK_OVERLAP}, MIN_CHUNK_SIZE={MIN_CHUNK_SIZE}"
)


class ContentType(Enum):
    """Content type for chunking strategy selection."""

    HTML = "html"
    MARKDOWN = "markdown"
    PLAIN = "plain"


# File extension mappings
_EXTENSION_TO_CONTENT_TYPE = {
    # HTML
    ".html": ContentType.HTML,
    ".htm": ContentType.HTML,
    ".xhtml": ContentType.HTML,
    # Markdown
    ".md": ContentType.MARKDOWN,
    ".markdown": ContentType.MARKDOWN,
    ".mdown": ContentType.MARKDOWN,
    ".mkd": ContentType.MARKDOWN,
    # Plain text (explicit)
    ".txt": ContentType.PLAIN,
    ".text": ContentType.PLAIN,
    # Code files (treat as plain)
    ".py": ContentType.PLAIN,
    ".js": ContentType.PLAIN,
    ".ts": ContentType.PLAIN,
    ".java": ContentType.PLAIN,
    ".c": ContentType.PLAIN,
    ".cpp": ContentType.PLAIN,
    ".go": ContentType.PLAIN,
    ".rs": ContentType.PLAIN,
    ".rb": ContentType.PLAIN,
    ".php": ContentType.PLAIN,
    ".sh": ContentType.PLAIN,
    ".bash": ContentType.PLAIN,
    ".zsh": ContentType.PLAIN,
    ".sql": ContentType.PLAIN,
    ".json": ContentType.PLAIN,
    ".yaml": ContentType.PLAIN,
    ".yml": ContentType.PLAIN,
    ".xml": ContentType.PLAIN,
    ".csv": ContentType.PLAIN,
    ".tsv": ContentType.PLAIN,
}


def detect_content_type_from_extension(
    file_path: Optional[str],
) -> Optional[ContentType]:
    """
    Detect content type from file extension.

    Args:
        file_path: Path to the file (can be full path or just filename)

    Returns:
        ContentType if extension is recognized, None otherwise
    """
    if not file_path:
        return None

    try:
        extension = Path(file_path).suffix.lower()
        return _EXTENSION_TO_CONTENT_TYPE.get(extension)
    except Exception:
        return None


def detect_content_type_from_heuristics(text: str) -> Tuple[ContentType, float]:
    """
    Detect content type using content heuristics.

    Args:
        text: The text content to analyze

    Returns:
        Tuple of (ContentType, confidence_score) where confidence is 0.0-1.0
    """
    if not text or len(text) < 10:
        return ContentType.PLAIN, 0.5

    # Sample first 5000 chars for efficiency
    sample = text[:5000]

    # Check HTML first (most specific patterns)
    html_score = _calculate_html_score(sample)
    if html_score >= HIGH_CONFIDENCE_THRESHOLD:
        return ContentType.HTML, html_score

    # Check Markdown
    markdown_score = _calculate_markdown_score(sample)
    if markdown_score >= HIGH_CONFIDENCE_THRESHOLD:
        return ContentType.MARKDOWN, markdown_score

    # Return the higher scoring type, or PLAIN if both are low
    if html_score > markdown_score and html_score > 0.3:
        return ContentType.HTML, html_score
    elif markdown_score > 0.3:
        return ContentType.MARKDOWN, markdown_score
    else:
        return ContentType.PLAIN, 0.6


def _calculate_html_score(text: str) -> float:
    """Calculate confidence score for HTML content."""
    score = 0.0
    indicators = 0

    # Strong indicators
    if re.search(r"<!DOCTYPE\s+html", text, re.IGNORECASE):
        score += 0.4
        indicators += 1

    if re.search(r"<html[\s>]", text, re.IGNORECASE):
        score += 0.3
        indicators += 1

    # Structural tags
    structural_tags = ["<head", "<body", "<div", "<span", "<p>", "<table", "<form"]
    for tag in structural_tags:
        if tag.lower() in text.lower():
            score += 0.1
            indicators += 1
            if indicators >= 5:
                break

    # Header tags
    if re.search(r"<h[1-6][\s>]", text, re.IGNORECASE):
        score += 0.15
        indicators += 1

    # Closing tags pattern
    if re.search(r"</\w+>", text):
        score += 0.1
        indicators += 1

    return min(score, 1.0)


def _calculate_markdown_score(text: str) -> float:
    """Calculate confidence score for Markdown content."""
    score = 0.0
    indicators = 0

    # Headers (# ## ###) - strong indicator
    header_matches = len(re.findall(r"^#{1,6}\s+.+", text, re.MULTILINE))
    if header_matches >= 3:
        score += 0.35
        indicators += 1
    elif header_matches >= 1:
        score += 0.2
        indicators += 1

    # Links [text](url) - strong indicator
    link_matches = len(re.findall(r"\[.+?\]\(.+?\)", text))
    if link_matches >= 2:
        score += 0.25
        indicators += 1
    elif link_matches >= 1:
        score += 0.15
        indicators += 1

    # Code blocks ``` - strong indicator
    if re.search(r"^```", text, re.MULTILINE):
        score += 0.2
        indicators += 1

    # Inline code `code`
    if re.search(r"`[^`]+`", text):
        score += 0.1
        indicators += 1

    # Lists (-, *, +, or numbered)
    list_matches = len(re.findall(r"^[\*\-\+]\s+", text, re.MULTILINE))
    list_matches += len(re.findall(r"^\d+\.\s+", text, re.MULTILINE))
    if list_matches >= 3:
        score += 0.15
        indicators += 1
    elif list_matches >= 1:
        score += 0.08
        indicators += 1

    # Bold/italic
    if re.search(r"\*\*.+?\*\*|__.+?__", text):
        score += 0.1
        indicators += 1

    # Blockquotes
    if re.search(r"^>\s+", text, re.MULTILINE):
        score += 0.1
        indicators += 1

    return min(score, 1.0)


def detect_content_type(text: str, file_path: Optional[str] = None) -> ContentType:
    """
    Detect content type using file extension (primary) and heuristics (fallback).

    Strategy:
    1. If file extension is available and recognized, use it as primary
    2. If no extension or generic extension (.txt), use heuristics
    3. Heuristics can override extension only with very high confidence

    Args:
        text: The text content
        file_path: Optional file path for extension-based detection

    Returns:
        Detected ContentType
    """
    # Try extension-based detection first
    extension_type = detect_content_type_from_extension(file_path)

    # Get heuristic-based detection
    heuristic_type, confidence = detect_content_type_from_heuristics(text)

    # If no extension or generic extension, use heuristics
    if extension_type is None:
        logger.debug(
            f"No file extension, using heuristics: {heuristic_type.value} "
            f"(confidence: {confidence:.2f})"
        )
        return heuristic_type

    # If extension suggests plain text but heuristics are very confident, override
    if extension_type == ContentType.PLAIN and confidence >= HIGH_CONFIDENCE_THRESHOLD:
        logger.debug(
            f"Extension suggests plain, but heuristics override with "
            f"{heuristic_type.value} (confidence: {confidence:.2f})"
        )
        return heuristic_type

    # Otherwise trust the extension
    logger.debug(f"Using extension-based content type: {extension_type.value}")
    return extension_type


def _get_html_splitter() -> HTMLHeaderTextSplitter:
    """Get HTML header splitter configured for h1, h2, h3."""
    headers_to_split_on = [
        ("h1", "Header 1"),
        ("h2", "Header 2"),
        ("h3", "Header 3"),
    ]
    return HTMLHeaderTextSplitter(headers_to_split_on=headers_to_split_on)


def _get_markdown_splitter() -> MarkdownHeaderTextSplitter:
    """Get Markdown header splitter configured for #, ##, ###."""
    headers_to_split_on = [
        ("#", "Header 1"),
        ("##", "Header 2"),
        ("###", "Header 3"),
    ]
    return MarkdownHeaderTextSplitter(
        headers_to_split_on=headers_to_split_on,
        strip_headers=False,
    )


def _get_plain_splitter() -> RecursiveCharacterTextSplitter:
    """Get plain text splitter using CHUNK_SIZE and CHUNK_OVERLAP constants."""
    return RecursiveCharacterTextSplitter(
        chunk_size=CHUNK_SIZE,
        chunk_overlap=CHUNK_OVERLAP,
        length_function=token_count,
        separators=["\n\n", "\n", ". ", ", ", " ", ""],
    )


def _apply_secondary_chunking(chunks: List[str]) -> List[str]:
    """
    Apply secondary chunking to ensure no chunk exceeds CHUNK_SIZE tokens.

    Used when primary splitters (HTML/Markdown) produce oversized chunks.
    """
    result = []
    secondary_splitter = _get_plain_splitter()

    for chunk in chunks:
        if token_count(chunk) > CHUNK_SIZE:
            # Split oversized chunk
            sub_chunks = secondary_splitter.split_text(chunk)
            result.extend(sub_chunks)
        else:
            result.append(chunk)

    return result


# ── Priority-order structural chunking (P2) ──────────────────────────────

# Atomic units extracted from markdown, in priority order:
#   1. code       — fenced code blocks, never split (atomic)
#   2. header     — markdown headers (# ## ###), forced new-chunk starters
#   3. paragraph  — blocks of text separated by blank lines
#   4. list_item  — bulleted/numbered list items, stay together
#   5. sentence   — . ! ? boundaries (fallback for oversized paragraphs)
#   6. clause     — , ; boundaries (last resort before word split)
#   7. word       — space boundaries (when a single sentence exceeds limit)
#   8. char       — never reached in practice

_CODE_FENCE_RE = re.compile(r"```[\s\S]*?```")
_HEADER_RE = re.compile(r"^#{1,6}\s+.+$", re.MULTILINE)
_LIST_ITEM_RE = re.compile(r"^[-*+]\s+.+$|^\d+\.\s+.+$", re.MULTILINE)
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")

_FORUM_BLOCKQUOTE_RE = re.compile(r"^>\s+", re.MULTILINE)
_FORUM_SEPARATOR_RE = re.compile(r"^---+\s*$", re.MULTILINE)


def _extract_units(
    content: str, page_type: Optional[str] = None
) -> List[tuple[str, str]]:
    """Parse markdown into atomic units in document order.

    Returns list of (unit_type, text) where unit_type is one of:
    "code", "header", "paragraph", "list_item", "forum_blockquote",
    "forum_sep".

    For page_type="forum":
      - Blockquote sections (> ...) are atomic units (keeps comment context)
      - --- separators force new-chunk boundaries
    """
    units: List[tuple[str, str]] = []
    lines = content.split("\n")
    i = 0
    current_paragraph: List[str] = []

    def flush_paragraph() -> None:
        nonlocal current_paragraph
        if current_paragraph:
            para = "\n".join(current_paragraph).strip()
            if para:
                units.append(("paragraph", para))
            current_paragraph = []

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        # ── Forum: --- separator — forced new-chunk boundary ─────────────
        if page_type == "forum" and _FORUM_SEPARATOR_RE.match(line):
            flush_paragraph()
            units.append(("forum_sep", line))
            i += 1
            continue

        # ── Forum: blockquote section — atomic unit ──────────────────────
        if page_type == "forum" and stripped.startswith(">"):
            flush_paragraph()
            block = [line]
            i += 1
            while i < len(lines) and lines[i].strip().startswith(">"):
                block.append(lines[i])
                i += 1
            units.append(("forum_blockquote", "\n".join(block)))
            continue

        # ── P1: Fenced code blocks — atomic, never split ─────────────────
        if stripped.startswith("```"):
            flush_paragraph()
            fence = stripped[:3]
            start = i
            i += 1
            while i < len(lines) and not lines[i].strip().startswith(fence):
                i += 1
            if i < len(lines):
                i += 1  # include closing fence
            block = "\n".join(lines[start:i])
            units.append(("code", block))
            continue

        # ── P2: Headers — forced new-chunk starters ──────────────────────
        if _HEADER_RE.match(line):
            flush_paragraph()
            header_block = [line]
            i += 1
            # Include following content until next header, blank line, or code fence
            while i < len(lines) and lines[i].strip():
                if lines[i].strip().startswith("```"):
                    break  # let code-block handler take over
                if _HEADER_RE.match(lines[i]):
                    break
                header_block.append(lines[i])
                i += 1
            units.append(("header", "\n".join(header_block)))
            continue

        # ── P4: List items — stay together ───────────────────────────────
        if _LIST_ITEM_RE.match(line):
            flush_paragraph()
            list_block = [line]
            i += 1
            while i < len(lines):
                nxt = lines[i].strip()
                # Next list item, or indented continuation of current item
                if _LIST_ITEM_RE.match(lines[i]) or (
                    nxt
                    and not nxt.startswith(("#", "```"))
                    and lines[i - 1].strip()
                    and not lines[i - 1].strip().startswith(("#", "```"))
                ):
                    list_block.append(lines[i])
                    i += 1
                else:
                    break
            units.append(("list_item", "\n".join(list_block)))
            continue

        # ── P3: Paragraphs — accumulate text separated by blank lines ────
        if stripped:
            current_paragraph.append(line)
        else:
            flush_paragraph()
        i += 1

    flush_paragraph()
    return units


def _split_oversized(text: str, max_tokens: int) -> List[tuple[str, str]]:
    """Split a single unit that exceeds max_tokens at sentence/clause/word boundaries.

    Returns list of (unit_type, text) sub-units, each <= max_tokens when possible.
    A single token > max_tokens (e.g., a very long URL) is returned as-is.
    """
    if token_count(text) < max_tokens:
        return [("sentence", text)]

    # P5: sentence boundaries
    sentences = _SENTENCE_SPLIT_RE.split(text)
    if len(sentences) > 1:
        result: List[tuple[str, str]] = []
        for s in sentences:
            result.extend(_split_oversized(s.strip(), max_tokens))
        return result

    # P6: clause boundaries
    clauses = re.split(r"(?<=[,;])\s+", text)
    if len(clauses) > 1:
        result = []
        for c in clauses:
            result.extend(_split_oversized(c.strip(), max_tokens))
        return result

    # P7: word boundaries
    words = text.split()
    if len(words) > 1:
        result = []
        for w in words:
            result.extend(_split_oversized(w, max_tokens))
        return result

    # P8: character boundary — single token too large, return as-is
    return [("char", text)]


def _last_n_tokens_text(lines: List[str], max_tokens: int) -> str:
    """Return the trailing portion of ``lines`` totaling <= max_tokens, for overlap."""
    if not lines or max_tokens <= 0:
        return ""
    joined = "\n".join(lines)
    if token_count(joined) <= max_tokens:
        return joined
    # Binary-ish walk from the end
    for cut in range(len(lines), 0, -1):
        candidate = "\n".join(lines[-cut:])
        if token_count(candidate) <= max_tokens:
            return candidate
    return lines[-1] if lines else ""


_FORUM_BLOCKQUOTE_RE = re.compile(r"^>\s+", re.MULTILINE)
_FORUM_SEPARATOR_RE = re.compile(r"^---+\s*$", re.MULTILINE)


def chunk_markdown(
    content: str,
    max_tokens: int = CHUNK_SIZE,
    overlap_tokens: int = CHUNK_OVERLAP,
    page_type: Optional[str] = None,
) -> List[str]:
    """Structural markdown chunking with atomic code blocks, headers, and list items.

    Priority order (P1–P8):
      P1: Fenced code blocks — atomic, never split
      P2: Markdown headers   — forced new-chunk starters
      P3: Paragraphs         — merge greedily up to max_tokens
      P4: List items         — stay together; list merges with preceding paragraph if fits
      P5: Sentences          — split oversized paragraphs at . ! ?
      P6: Clauses            — split at , ;
      P7: Words              — split at spaces
      P8: Characters         — never reached in practice

    Page-type-aware behavior:
      - forum: blockquote sections (> ...) are atomic units; --- separators
               flow as normal text (do not force new-chunk boundaries, which
               would over-split small chunks)
      - documentation: headers are forced new-chunk starters (already P2)
      - product/collection/listing: list items stay together (already P4)
      - article/service: standard structural chunking

    This differs from RecursiveCharacterTextSplitter in that headers and code
    blocks are *forced* new-chunk starters (never merged with preceding
    content), preserving section boundaries and code integrity.
    """
    if not content or not content.strip():
        return []

    text_tokens = token_count(content)
    if text_tokens <= max_tokens:
        return [content]

    # Step 1: extract atomic units in document order
    units = _extract_units(content, page_type=page_type)

    # Step 2: split any oversized units at sentence/clause/word boundaries
    final_units: List[tuple[str, str]] = []
    for unit_type, text in units:
        if token_count(text) >= max_tokens:
            final_units.extend(_split_oversized(text, max_tokens))
        else:
            final_units.append((unit_type, text))

    # Step 3: greedy packing respecting max_tokens.
    # We track the actual token count of the joined text so that spaces,
    # newlines, and other inter-unit whitespace are counted correctly.
    chunks: List[str] = []
    current_text = ""

    for unit_type, text in final_units:
        # Headers and code blocks always start a new chunk when we have content
        if unit_type in ("header", "code") and current_text:
            chunks.append(current_text)
            current_text = ""

        # Forum separators ("---") and blockquotes are NOT forced boundaries.
        # Both are packed greedily with adjacent units up to max_tokens.
        # This prevents over-splitting short comments into sub-100-token
        # chunks — benchmark research showed that dropping from ~400-token
        # chunks to ~43-token chunks dropped retrieval accuracy to 54%.
        # A forum comment at ~100 tokens should share a chunk with its
        # neighbors until the chunk approaches the 400-token target.

        tentative = current_text + ("\n" if current_text else "") + text
        if token_count(tentative) >= max_tokens and current_text:
            chunks.append(current_text)
            current_text = text
        else:
            current_text = tentative

    if current_text:
        chunks.append(current_text)

    # Safety net: if we ended up with a single chunk but the original text
    # exceeded the token limit (e.g., a token was lost at a sentence-split
    # boundary), force a split using the secondary recursive splitter.
    if len(chunks) == 1 and token_count(text) > max_tokens:
        chunks = _apply_secondary_chunking(chunks)

    # Filter degenerate chunks (below MIN_CHUNK_SIZE), but never empty the result
    if MIN_CHUNK_SIZE > 0 and len(chunks) > 1:
        kept = [c for c in chunks if token_count(c) >= MIN_CHUNK_SIZE]
        if kept:
            chunks = kept

    return chunks if chunks else [content]


def chunk_text(
    text: str,
    content_type: Optional[ContentType] = None,
    file_path: Optional[str] = None,
    page_type: Optional[str] = None,
) -> List[str]:
    """
    Split text into chunks using appropriate splitter for content type.

    Args:
        text: The text to chunk
        content_type: Optional explicit content type (auto-detected if not provided)
        file_path: Optional file path for content type detection
        page_type: Optional page type (article, forum, product, collection,
                   listing, documentation, service) for page-type-aware
                   chunking strategy

    Returns:
        List of text chunks, each approximately <= CHUNK_SIZE tokens
    """
    if not text or not text.strip():
        return []

    # Short text doesn't need chunking
    text_tokens = token_count(text)
    if text_tokens <= CHUNK_SIZE:
        return [text]

    # Detect content type if not provided
    if content_type is None:
        content_type = detect_content_type(text, file_path)

    logger.debug(
        f"Chunking text with content type: {content_type.value}, "
        f"page_type: {page_type}"
    )

    # Select appropriate splitter
    chunks: List[str]
    if content_type == ContentType.HTML:
        # Raw HTML: split on HTML headers first (pre-markdown-conversion),
        # then structurally chunk each section.
        html_splitter = _get_html_splitter()
        docs = html_splitter.split_text(text)
        raw_sections = [
            doc.page_content if hasattr(doc, "page_content") else str(doc)
            for doc in docs
        ]
        chunks = []
        for section in raw_sections:
            chunks.extend(
                chunk_markdown(section, CHUNK_SIZE, CHUNK_OVERLAP, page_type=page_type)
            )
    elif content_type == ContentType.MARKDOWN:
        # Markdown: structural chunking with atomic code blocks, headers, list items
        chunks = chunk_markdown(text, CHUNK_SIZE, CHUNK_OVERLAP, page_type=page_type)
    else:
        # Plain text: same structural chunker, no header/code assumptions
        chunks = chunk_markdown(text, CHUNK_SIZE, CHUNK_OVERLAP, page_type=page_type)

    # Apply secondary chunking as safety net (should rarely trigger now)
    chunks = _apply_secondary_chunking(chunks)

    # Filter out empty chunks
    chunks = [c.strip() for c in chunks if c and c.strip()]

    # Drop chunks below the minimum token threshold...
    if MIN_CHUNK_SIZE > 0 and len(chunks) > 1:
        kept = [c for c in chunks if token_count(c) >= MIN_CHUNK_SIZE]
        if kept:
            dropped = len(chunks) - len(kept)
            if dropped > 0:
                logger.debug(
                    f"Dropped {dropped} chunk(s) below MIN_CHUNK_SIZE={MIN_CHUNK_SIZE} tokens"
                )
            chunks = kept

    logger.debug(f"Created {len(chunks)} chunks from {text_tokens} tokens")
    return chunks


def describe_chunks(chunks: List[str]) -> Dict[str, Any]:
    """Shape/cost stats for one chunking (N2a). Quality needs judged data."""
    if not chunks:
        return {"chunks": 0, "avg_tokens": 0, "max_tokens": 0, "total_tokens": 0}
    sizes = [token_count(c) for c in chunks]
    total = sum(sizes)
    return {
        "chunks": len(chunks),
        "avg_tokens": total // len(chunks),
        "max_tokens": max(sizes),
        "total_tokens": total,
    }


def chunk_text_with_coords(
    text: str,
    content_type: Optional[ContentType] = None,
    file_path: Optional[str] = None,
    page_type: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    Split text into chunks and return each chunk with character coordinates.

    Returns a list of dicts with keys:
        - text: the chunk text
        - char_start: character offset in the original text
        - char_end: character offset (exclusive) in the original text
        - chunk_index: 0-based index

    Coordinates are approximate: each chunk's text is located in the original
    text by searching forward from the previous chunk's end. Overlapping
    chunks (from the splitter's overlap parameter) may have slightly
    imprecise coordinates, which is acceptable for citation display.
    """
    chunk_strings = chunk_text(
        text, content_type=content_type, file_path=file_path, page_type=page_type
    )
    if not chunk_strings:
        return []

    result: List[Dict[str, Any]] = []
    search_from = 0
    for idx, chunk_text_str in enumerate(chunk_strings):
        # Find the chunk in the original text, searching from where we left off
        pos = text.find(chunk_text_str, search_from)
        if pos == -1:
            # Chunk not found (e.g., due to whitespace normalization); use
            # the previous chunk's end as fallback
            pos = search_from
        end = pos + len(chunk_text_str)
        result.append(
            {
                "text": chunk_text_str,
                "char_start": pos,
                "char_end": end,
                "chunk_index": idx,
            }
        )
        search_from = end

    return result
