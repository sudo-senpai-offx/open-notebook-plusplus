"""Selective OCR: which pages get read, and what survives.

The two properties that matter are that text already extracted is never
re-read, and that output the engine is unsure about is discarded rather
than stored. Both were wrong in the first version and both failed
silently - the detector classified 77% of a real library as needing OCR
because it counted text *objects* instead of characters, and every page
came back empty because a confidence of "92.16" was parsed with int().

These tests use synthetic PDFs so they run anywhere. The behaviour on
real documents is covered by tests/test_ocr_on_real_documents.py.
"""

from pathlib import Path

from open_notebook.ocr.analyze import (
    GRAPHICAL_AREA_RATIO,
    MIN_REGION_AREA_RATIO,
    MIN_TEXT_CHARS,
    PageKind,
    _merge_regions,
    _union_area,
    analyse,
)


def _make_pdf(path: Path, pages: list[str]) -> Path:
    """Write a small PDF whose pages carry the given text.

    Hand-built because this pypdfium2 build exposes no text-creation API,
    and the object numbering and xref offsets have to be exactly right -
    an earlier version of this fixture produced a file pdfium refused to
    open, so every classification test was failing for reasons that had
    nothing to do with the code under test.
    """
    count = 3 + 2 * len(pages)
    kids = " ".join(f"{4 + 2 * i} 0 R" for i in range(len(pages)))
    objects: dict[int, str] = {
        1: "<< /Type /Catalog /Pages 2 0 R >>",
        2: f"<< /Type /Pages /Kids [{kids}] /Count {len(pages)} >>",
        3: "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    }
    for index, text in enumerate(pages):
        page_id, content_id = 4 + 2 * index, 5 + 2 * index
        stream = f"BT /F1 12 Tf 72 700 Td ({text}) Tj ET"
        objects[page_id] = (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Resources << /Font << /F1 3 0 R >> >> /Contents {content_id} 0 R >>"
        )
        objects[content_id] = (
            f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream"
        )

    out = bytearray(b"%PDF-1.4\n")
    offsets: dict[int, int] = {}
    for number in range(1, count + 1):
        offsets[number] = len(out)
        out += f"{number} 0 obj\n".encode()
        out += objects[number].encode("latin-1")
        out += b"\nendobj\n"
    xref_at = len(out)
    out += f"xref\n0 {count + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for number in range(1, count + 1):
        out += f"{offsets[number]:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {count + 1} /Root 1 0 R >>\nstartxref\n{xref_at}\n%%EOF\n"
    ).encode()
    path.write_bytes(bytes(out))
    return path


class TestClassification:
    def test_a_text_page_is_skipped(self, tmp_path):
        long_enough = "x" * (MIN_TEXT_CHARS + 50)
        pdf = _make_pdf(tmp_path / "text.pdf", [long_enough])
        analysis = analyse(pdf)
        assert analysis.readable
        assert analysis.pages[0].kind is PageKind.TEXT
        assert not analysis.needs_ocr

    def test_a_page_with_no_text_is_graphical(self, tmp_path):
        pdf = _make_pdf(tmp_path / "empty.pdf", [""])
        analysis = analyse(pdf)
        assert analysis.pages[0].kind is PageKind.GRAPHICAL
        assert analysis.needs_ocr

    def test_a_short_page_counts_as_graphical(self, tmp_path):
        """Below the threshold it is not reliably typeset, so read it."""
        pdf = _make_pdf(tmp_path / "short.pdf", ["Page 4"])
        analysis = analyse(pdf)
        assert analysis.pages[0].text_chars < MIN_TEXT_CHARS
        assert analysis.pages[0].kind is PageKind.GRAPHICAL

    def test_text_length_is_counted_in_characters_not_objects(self, tmp_path):
        """The regression: object count said nothing about how much text.

        One page holding a full paragraph is a handful of text objects,
        so counting objects put every ordinary page below the threshold
        and flagged the entire library for OCR.
        """
        long_enough = "y" * (MIN_TEXT_CHARS + 50)
        pdf = _make_pdf(tmp_path / "para.pdf", [long_enough])
        page = analyse(pdf).pages[0]
        assert page.text_chars >= MIN_TEXT_CHARS
        assert page.kind is PageKind.TEXT


class TestUnreadableFiles:
    def test_a_non_pdf_is_reported_not_crashed(self, tmp_path):
        junk = tmp_path / "notreally.pdf"
        junk.write_bytes(b"this is ASCII text with no line terminators")
        analysis = analyse(junk)
        assert not analysis.readable
        assert "not a readable PDF" in (analysis.error or "")
        assert not analysis.needs_ocr

    def test_summary_exposes_the_error(self, tmp_path):
        junk = tmp_path / "x.pdf"
        junk.write_bytes(b"nope")
        summary = analyse(junk).summary()
        assert summary["readable"] is False
        assert summary["error"]


class TestGeometry:
    def test_union_counts_overlap_once(self):
        # Two boxes that fully overlap cover the same area.
        assert _union_area([(0, 0, 10, 10), (0, 0, 10, 10)]) == 100

    def test_union_sums_disjoint_boxes(self):
        assert _union_area([(0, 0, 10, 10), (20, 0, 30, 10)]) == 200

    def test_nearby_boxes_merge(self):
        merged = _merge_regions([(0, 0, 100, 100), (102, 0, 200, 100)], 612, 792)
        assert len(merged) == 1

    def test_distant_boxes_stay_separate(self):
        merged = _merge_regions([(0, 0, 100, 100), (400, 0, 500, 100)], 612, 792)
        assert len(merged) == 2

    def test_tiny_regions_are_dropped(self):
        # A logo should not cost a render and a tesseract startup.
        merged = _merge_regions([(0, 0, 3, 3)], 612, 792)
        assert not merged

    def test_area_ratio_threshold_is_respected(self):
        page = 612 * 792
        big = _merge_regions([(0, 0, 300, 400)], 612, 792)
        assert big
        ratio = (big[0][2] - big[0][0]) * (big[0][3] - big[0][1]) / page
        assert ratio >= MIN_REGION_AREA_RATIO


class TestTSVParsing:
    """The confidence gate, tested without needing tesseract installed."""

    def _tsv(self, rows: list[tuple[str, str]]) -> str:
        head = (
            "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\t"
            "left\ttop\twidth\theight\tconf\ttext"
        )
        lines = [head]
        for i, (conf, text) in enumerate(rows, start=1):
            lines.append(f"5\t1\t1\t1\t{i}\t{i}\t0\t0\t10\t10\t{conf}\t{text}")
        return "\n".join(lines)

    def test_confidence_is_a_float_not_an_int(self):
        """The regression that emptied every page.

        Tesseract writes "92.167656". int() raises, the word was
        discarded, and the library OCR'd to nothing - which looked
        exactly like a confidence filter doing its job.
        """
        from open_notebook.ocr.engine import _tsv_to_text

        text, conf = _tsv_to_text(self._tsv([("92.167656", "Eukaryotic")]))
        assert "Eukaryotic" in text
        assert conf > 90

    def test_low_confidence_words_are_dropped(self):
        from open_notebook.ocr.engine import MIN_WORD_CONFIDENCE, _tsv_to_text

        rows = [
            ("95", "protein"),
            ("20", "KINA"),
            ("25", "diagram"),
        ]
        text, _ = _tsv_to_text(self._tsv(rows))
        assert "protein" in text
        assert "KINA" not in text
        assert "diagram" not in text
        assert MIN_WORD_CONFIDENCE > 20

    def test_a_page_of_low_confidence_is_discarded_entirely(self):
        """A figure is not text, and its output is worse than nothing."""
        from open_notebook.ocr.engine import _tsv_to_text

        rows = [(f"{30 + i}", "junk") for i in range(10)]
        text, conf = _tsv_to_text(self._tsv(rows))
        assert text == ""
        assert conf < 60

    def test_structural_rows_are_ignored(self):
        from open_notebook.ocr.engine import _tsv_to_text

        tsv = (
            "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\t"
            "left\ttop\twidth\theight\tconf\ttext\n"
            "1\t1\t0\t0\t0\t0\t0\t0\t100\t100\t-1\t\n"
            "2\t1\t1\t0\t0\t0\t0\t0\t100\t100\t-1\t\n"
            "5\t1\t1\t1\t1\t1\t0\t0\t10\t10\t95.0\tReal\n"
        )
        text, conf = _tsv_to_text(tsv)
        assert text.strip() == "Real"

    def test_negative_confidence_rows_are_skipped(self):
        from open_notebook.ocr.engine import _tsv_to_text

        text, _ = _tsv_to_text(self._tsv([("-1", ""), ("90", "word")]))
        assert text.strip() == "word"


class TestConfiguration:
    def test_default_languages_include_japanese(self, monkeypatch):
        from open_notebook.ocr.engine import ocr_languages

        monkeypatch.delenv("OPEN_NOTEBOOK_OCR_LANG", raising=False)
        assert ocr_languages() == "eng+jpn"

    def test_language_list_is_normalised(self, monkeypatch):
        from open_notebook.ocr.engine import ocr_languages

        monkeypatch.setenv("OPEN_NOTEBOOK_OCR_LANG", "eng, jpn , deu")
        assert ocr_languages() == "eng+jpn+deu"

    def test_a_malformed_language_list_falls_back(self, monkeypatch):
        from open_notebook.ocr.engine import ocr_languages

        monkeypatch.setenv("OPEN_NOTEBOOK_OCR_LANG", "not-a-language-list")
        assert ocr_languages() == "eng"

    def test_dpi_is_clamped_to_a_sane_range(self, monkeypatch):
        from open_notebook.ocr.engine import ocr_dpi

        monkeypatch.setenv("OPEN_NOTEBOOK_OCR_DPI", "50")
        assert ocr_dpi() >= 150
        monkeypatch.setenv("OPEN_NOTEBOOK_OCR_DPI", "5000")
        assert ocr_dpi() <= 400

    def test_dpi_defaults_to_the_balance_point(self, monkeypatch):
        from open_notebook.ocr.engine import DEFAULT_DPI, ocr_dpi

        monkeypatch.delenv("OPEN_NOTEBOOK_OCR_DPI", raising=False)
        assert ocr_dpi() == DEFAULT_DPI == 200

    def test_dpi_falls_back_on_nonsense(self, monkeypatch):
        from open_notebook.ocr.engine import DEFAULT_DPI, ocr_dpi

        monkeypatch.setenv("OPEN_NOTEBOOK_OCR_DPI", "high")
        assert ocr_dpi() == DEFAULT_DPI

    def test_master_switch(self, monkeypatch):
        from open_notebook.ocr.engine import ocr_enabled

        monkeypatch.setenv("OPEN_NOTEBOOK_OCR_ENABLED", "false")
        assert ocr_enabled() is False
        monkeypatch.setenv("OPEN_NOTEBOOK_OCR_ENABLED", "true")
        assert ocr_enabled() is True


class TestDegradation:
    """Nothing about OCR may make a source un-ingestable."""

    def test_missing_engine_is_reported_not_raised(self, monkeypatch):
        from open_notebook.ocr.engine import available

        monkeypatch.setenv("OPEN_NOTEBOOK_OCR_BIN", "definitely-not-installed")
        ok, detail = available()
        assert ok is False
        assert "not on PATH" in detail or "Termux" in detail

    def test_disabled_engine_is_reported(self, monkeypatch):
        from open_notebook.ocr.engine import available

        monkeypatch.setenv("OPEN_NOTEBOOK_OCR_ENABLED", "false")
        ok, detail = available()
        assert ok is False
        assert "off" in detail

    def test_ocr_of_a_missing_file_returns_an_error(self, tmp_path, monkeypatch):
        from open_notebook.ocr.engine import ocr_document

        monkeypatch.setenv("OPEN_NOTEBOOK_OCR_BIN", "tesseract")
        result = ocr_document(tmp_path / "nope.pdf")
        assert not result.ok
        assert result.error

    def test_probe_describes_without_doing(self, tmp_path, monkeypatch):
        from open_notebook.ocr.engine import probe

        monkeypatch.setenv("OPEN_NOTEBOOK_OCR_ENABLED", "false")
        summary = probe(_make_pdf(tmp_path / "p.pdf", ["hello"]))
        assert summary["available"] is False
        assert "total_pages" in summary


class TestInvariant:
    def test_graphical_ratio_is_a_sane_constant(self):
        # If this ever drops below ~0.05, every page with a header logo
        # becomes an OCR candidate and the speed argument collapses.
        assert 0.05 <= GRAPHICAL_AREA_RATIO <= 0.5
