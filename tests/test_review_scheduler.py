"""P4 scheduler seam: real fsrs behavior, store injected later.

Retention 0.85 default (locked R5Q4); per-deck dial later. Scheduler state
travels as JSON (py-fsrs to_json/from_json) so SurrealDB storage is plain
text in the next slice.
"""

from fsrs import Card, Rating, Scheduler, State


def _scheduler():
    return Scheduler(desired_retention=0.85)


def test_first_good_schedules_forward():
    card, _log = _scheduler().review_card(Card(), Rating.Good)
    assert card.due > card.last_review


def test_lapse_returns_to_relearning():
    scheduler = _scheduler()
    card = Card()
    for _ in range(5):
        card, _ = scheduler.review_card(card, Rating.Good)
        if card.state == State.Review:
            break
    assert card.state == State.Review
    card, _ = scheduler.review_card(card, Rating.Again)
    assert card.state == State.Relearning


def test_ratings_accepted_and_serializable():
    scheduler = _scheduler()
    for rating in (Rating.Again, Rating.Hard, Rating.Good, Rating.Easy):
        card, log = scheduler.review_card(Card(), rating)
        assert log.rating == rating
        assert card.to_json()
