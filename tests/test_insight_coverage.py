"""The coverage checker has to be able to tell the two cases apart.

A diagnostic that reports "full coverage" for an insight that only saw the
front matter is worse than no diagnostic: it is a green light over a hole. So
the measurement itself is tested here, on documents where the answer is known
in advance - one where the tail says something the head does not, and one
where the insight plainly never saw the tail.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _load():
    path = REPO / "scripts" / "insight-coverage.py"
    spec = importlib.util.spec_from_file_location("insight_coverage", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


cov = _load()


def _document(paragraphs: int = 200) -> str:
    """A long document whose last paragraph says something the rest does not."""
    body = " ".join(["ordinary"] * 60) + "."
    text = "\n\n".join(f"Paragraph {i}. {body}" for i in range(paragraphs))
    return (
        f"{text}\n\nThe conclusion argues that negotiating the sale of the "
        f"cliched aphorisms requires disproportionate patience."
    )


class TestTheMeasurement:
    def test_a_late_phrase_is_found(self):
        """A run of three words is the unit of evidence.

        One word is a coincidence; three in a row is a phrase the document
        used, and the only way an insight can contain it is by having read
        that part of the document.
        """
        assert "negotiating the sale" in cov._trigrams(_document())

    def test_a_phrase_in_the_head_is_not_late(self):
        """The same phrase in the part the old path *did* see is not evidence."""
        head = cov._trigrams(cov._truncate_to_tokens(_document(), 200))
        assert "paragraph ordinary ordinary" in head
        assert "negotiating the sale" not in head

    def test_the_old_path_saw_only_the_start(self):
        """The truncation point is where the provider would have cut."""
        document = _document(paragraphs=400)
        cut = cov._truncation_point(document, 8192, 700)
        old_view = cov._truncate_to_tokens(document, cut)
        assert "negotiating" not in old_view.lower()
        assert len(old_view) < len(document) / 2

    def test_a_trigram_needs_three_words(self):
        """Two words in a row is a coincidence; three is a phrase."""
        assert "negotiating the" not in cov._trigrams(_document())
        assert "negotiating the sale" in cov._trigrams(_document())


class TestTheReport:
    """The report is what a person reads, so it has to say the right thing."""

    def test_an_insight_that_never_saw_the_tail_scores_nothing(self):
        late = cov._trigrams(_document()) - cov._trigrams(
            cov._truncate_to_tokens(_document(), 200)
        )
        assert late, "the document should have late phrases"
        only_the_head = "a summary of the opening paragraphs and the cover"
        assert not (late & cov._trigrams(only_the_head))

    def test_an_insight_that_saw_the_tail_is_credited(self):
        late = cov._trigrams(_document()) - cov._trigrams(
            cov._truncate_to_tokens(_document(), 200)
        )
        answer = (
            "The document concludes by arguing that negotiating the sale of "
            "the cliched aphorisms requires disproportionate patience."
        )
        hits = late & cov._trigrams(answer)
        assert hits, "the answer quotes the tail, so it must be credited"

    def test_quoting_the_noise_also_counts(self):
        """A badly extracted tail is still the tail.

        An insight that quotes the OCR noise has still read the part of the
        document the old path never saw, and the metric is about coverage -
        not about whether the document was worth reading.
        """
        document = "alpha bravo charlie delta echo foxtrot golf hotel india"
        late = cov._trigrams(document)
        assert "bravo charlie delta" in late
