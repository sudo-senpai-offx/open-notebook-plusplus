"""Metrics are derived per request from event logs. No stored counters."""

from datetime import datetime, timedelta, timezone

from open_notebook.metrics.summary import compute_summary

NOW = datetime(2026, 10, 12, 12, 0, 0, tzinfo=timezone.utc)


def _rev(day_offset, rating="good", duration=4000):
    return {
        "rating": rating,
        "reviewed_at": (NOW - timedelta(days=day_offset)).isoformat(),
        "duration_ms": duration,
    }


def _sess(day_offset, minutes=25):
    start = NOW - timedelta(days=day_offset, minutes=minutes)
    return {
        "started_at": start.isoformat(),
        "ended_at": (start + timedelta(minutes=minutes)).isoformat(),
    }


def test_empty_history_is_zeroed():
    s = compute_summary(reviews=[], sessions=[], due_count=0, now=NOW)
    assert s == {
        "due_count": 0,
        "reviews_today": 0,
        "lapses_7d": 0,
        "median_ms": None,
        "streak_days": 0,
        "focus_minutes_7d": 0,
    }


def test_counts_and_lapses():
    reviews = [_rev(0), _rev(0, rating="again"), _rev(1), _rev(8, rating="again")]
    s = compute_summary(reviews=reviews, sessions=[], due_count=5, now=NOW)
    assert s["due_count"] == 5
    assert s["reviews_today"] == 2
    assert s["lapses_7d"] == 1


def test_median_and_streak():
    reviews = [
        _rev(0, duration=2000),
        _rev(0, duration=4000),
        _rev(0, duration=6000),
        _rev(1),
        _rev(2),
    ]
    s = compute_summary(reviews=reviews, sessions=[], due_count=0, now=NOW)
    assert s["median_ms"] == 4000
    assert s["streak_days"] == 3


def test_streak_survives_yesterday_only():
    s = compute_summary(reviews=[_rev(1), _rev(2)], sessions=[], due_count=0, now=NOW)
    assert s["streak_days"] == 2


def test_streak_broken_by_gap():
    s = compute_summary(reviews=[_rev(0), _rev(3)], sessions=[], due_count=0, now=NOW)
    assert s["streak_days"] == 1


def test_focus_minutes_windowed():
    sessions = [_sess(0, 25), _sess(1, 50), _sess(9, 25)]
    s = compute_summary(reviews=[], sessions=sessions, due_count=0, now=NOW)
    assert s["focus_minutes_7d"] == 75
