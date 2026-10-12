"""N2a: chunk shape/cost comparison (quality needs a judged fixture)."""

from open_notebook.utils.chunking import describe_chunks
from open_notebook.utils.token_utils import token_count


def test_describe_empty():
    assert describe_chunks([]) == {
        "chunks": 0,
        "avg_tokens": 0,
        "max_tokens": 0,
        "total_tokens": 0,
    }


def test_describe_counts_and_scales():
    chunks = ["a b c", "d e f g h"]
    stats = describe_chunks(chunks)
    assert stats["chunks"] == 2
    assert stats["total_tokens"] == sum(token_count(c) for c in chunks)
    assert stats["max_tokens"] >= stats["avg_tokens"] > 0
