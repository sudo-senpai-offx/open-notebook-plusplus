"""QC scoring: recall@k over result id lists (pure, no network)."""

from open_notebook.eval.qc import recall_at_k, summarize


def test_hit_when_expected_in_top_k():
    results = [{"id": "a"}, {"id": "b"}, {"id": "c"}]
    assert recall_at_k(results, ["b"], k=3) == 1.0
    assert recall_at_k(results, ["b"], k=1) == 0.0


def test_miss_and_empty():
    assert recall_at_k([{"id": "a"}], ["zzz"], k=5) == 0.0
    assert recall_at_k([], ["a"], k=5) == 0.0


def test_multiple_expected_all_must_hit():
    results = [{"id": "a"}, {"id": "b"}]
    assert recall_at_k(results, ["a", "b"], k=2) == 1.0
    assert recall_at_k(results, ["a", "zzz"], k=5) == 0.0


def test_summarize_counts_and_rate():
    outcomes = [
        {"question": "q1", "hit": True},
        {"question": "q2", "hit": False},
        {"question": "q3", "hit": True},
    ]
    summary = summarize(outcomes)
    assert summary == {"total": 3, "hits": 2, "recall": 2 / 3}
    assert summarize([]) == {"total": 0, "hits": 0, "recall": 0.0}
