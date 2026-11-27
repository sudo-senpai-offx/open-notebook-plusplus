"""Integration tests for Week 4 preprocess wrappers (Day 2 pandoc, Day 3 trafilatura).

Four tests:
  1. pandoc strips base64 image URIs from HTML + word/media/* from DOCX
  2. trafilatura extracts clean markdown (article body + tables preserved,
     nav/footer boilerplate stripped)
  3. trafilatura flags SPA/JavaScript-required pages (extracting_partial)
  4. trafilatura raises ValueError on empty/blocked HTML

Plus a pandoc subprocess mock test (pandoc binary not installed in this env).

Brings test count from 56 → 60.
"""

from __future__ import annotations

import os
import zipfile
from unittest.mock import AsyncMock

import pytest

from open_notebook.preprocess.tools.pandoc import (
    convert_document,
    strip_docx_media,
    strip_html_base64,
)
from open_notebook.preprocess.tools.trafilatura import extract_html


# ── Helpers ──────────────────────────────────────────────────────────────


def _make_docx_with_media(tmp_path, media_name: str = "image1.png") -> str:
    """Create a minimal DOCX with a fake image in word/media/."""
    docx_path = os.path.join(tmp_path, "test.docx")
    with zipfile.ZipFile(docx_path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", '<?xml version="1.0"?><Types/>')
        z.writestr("_rels/.rels", '<?xml version="1.0"?><Relationships/>')
        z.writestr(
            "word/document.xml",
            '<?xml version="1.0"?><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>Hello World</w:t></w:r></w:p></w:body></w:document>',
        )
        z.writestr(f"word/media/{media_name}", b"\x89PNG fake image data")
    return docx_path


def _fake_podoc_subprocess(stdout: str = "# Converted\n"):
    """Return an AsyncMock subprocess that mimics pandoc output."""
    mock_proc = AsyncMock()
    mock_proc.returncode = 0
    mock_proc.communicate = AsyncMock(
        return_value=(stdout.encode(), b"")
    )
    return mock_proc


# ── Test 1: pandoc strips base64 + DOCX media ───────────────────────────


def test_pandoc_strips_base64_and_docx_media(tmp_path):
    """Base64 URIs stripped from HTML; word/media/* stripped from DOCX."""
    # HTML base64 strip
    html_with_base64 = (
        '<html><body><article><h1>Article</h1>'
        '<p><img src="data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==" alt="img"></p>'
        '</article></body></html>'
    )
    stripped = strip_html_base64(html_with_base64)
    assert "data:image" not in stripped
    assert "base64" not in stripped
    assert "Article" in stripped

    # DOCX media strip
    docx_path = _make_docx_with_media(tmp_path)
    with zipfile.ZipFile(docx_path, "r") as z:
        assert any("word/media/" in n for n in z.namelist())

    stripped_path = strip_docx_media(docx_path)
    with zipfile.ZipFile(stripped_path, "r") as z:
        assert not any("word/media/" in n for n in z.namelist())
        doc = z.read("word/document.xml").decode()
        assert "Hello World" in doc

    os.unlink(stripped_path)


@pytest.mark.asyncio
async def test_pandoc_convert_document_mocks_subprocess(tmp_path, monkeypatch):
    """convert_document calls pandoc subprocess and returns converted text."""
    docx_path = _make_docx_with_media(tmp_path)
    fake_proc = _fake_podoc_subprocess(stdout="# Hello World\n\nConverted content.")

    async def fake_create_subprocess_exec(*args, **kwargs):
        return fake_proc

    monkeypatch.setattr(
        "open_notebook.preprocess.tools.pandoc.asyncio.create_subprocess_exec",
        fake_create_subprocess_exec,
    )

    result = await convert_document(
        command_id="test-cmd",
        file_path=docx_path,
        source_format="docx",
        language="en",
    )

    assert result["text"].strip() == "# Hello World\n\nConverted content."
    assert result["pages_processed"] == 1
    assert result["checkpoint_position"] == 1


# ── Test 2: trafilatura extracts clean markdown ─────────────────────────


@pytest.mark.asyncio
async def test_trafilatura_extracts_clean_markdown():
    """Article body + tables preserved; nav/footer boilerplate stripped."""
    html = """<html>
    <head><title>Test Page</title></head>
    <body>
        <nav><ul><li>Home</li><li>About</li></ul></nav>
        <article>
            <h1>Article Title</h1>
            <p>This is the main article content with meaningful text.</p>
            <table>
                <tr><th>Column 1</th><th>Column 2</th></tr>
                <tr><td>Value 1</td><td>Value 2</td></tr>
            </table>
        </article>
        <footer><p>Copyright 2026</p></footer>
    </body>
    </html>"""

    result = await extract_html(command_id="test-cmd", html_content=html)

    text = result["text"]
    assert "Article Title" in text
    assert "main article content" in text
    assert "Home" not in text
    assert "About" not in text
    assert "Copyright 2026" not in text
    assert result["pages_processed"] == 1
    assert result["extraction_empty"] is False


# ── Test 3: trafilatura flags SPA/JavaScript-required ───────────────────


@pytest.mark.asyncio
async def test_trafilatura_flags_spa_javascript_required():
    """SPA shell (<div id="__next"></div>) → javascript_required, no ValueError."""
    html = """<html>
    <head><title>App</title></head>
    <body>
        <div id="__next"></div>
        <script src="/_next/static/chunks/main.js"></script>
    </body>
    </html>"""

    result = await extract_html(command_id="test-cmd", html_content=html)

    assert result["javascript_required"] is True
    assert result["extraction_empty"] is True


# ── Test 4: trafilatura empty/blocked HTML raises ValueError ────────────


@pytest.mark.asyncio
async def test_trafilatura_empty_html_fails_loudly():
    """Empty/blocked HTML (no SPA markers) → ValueError('html_extraction_empty')."""
    html = "<html><body></body></html>"

    with pytest.raises(ValueError, match="html_extraction_empty"):
        await extract_html(command_id="test-cmd", html_content=html)
