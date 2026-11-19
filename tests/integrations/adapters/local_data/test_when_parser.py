"""Step 15: the owner's time words become a date in code, never by model arithmetic."""

from __future__ import annotations

from datetime import datetime

import pytest

from integrations.adapters.local_data.when_parser import (
    WhenError,
    describe,
    parse_day_range,
    parse_when,
)

NOW = datetime(2026, 11, 19, 14, 30)  # a Thursday afternoon


@pytest.mark.parametrize(
    ("words", "expected"),
    [
        ("on Friday", datetime(2026, 11, 20, 9, 0)),
        ("Friday", datetime(2026, 11, 20, 9, 0)),
        ("next friday at 5pm", datetime(2026, 11, 20, 17, 0)),
        ("on Thursday", datetime(2026, 11, 26, 9, 0)),  # today's weekday = next week
        ("tomorrow at 9", datetime(2026, 11, 20, 9, 0)),
        ("tomorrow 9:30 pm", datetime(2026, 11, 20, 21, 30)),
        ("day after tomorrow", datetime(2026, 11, 21, 9, 0)),
        ("today at 17:45", datetime(2026, 11, 19, 17, 45)),
        ("tonight", datetime(2026, 11, 19, 20, 0)),
        ("Sunday evening", datetime(2026, 11, 22, 18, 0)),
        ("at 8am", datetime(2026, 11, 20, 8, 0)),  # already passed today -> tomorrow
        ("at 4pm", datetime(2026, 11, 19, 16, 0)),
        ("in 20 minutes", datetime(2026, 11, 19, 14, 50)),
        ("in an hour", datetime(2026, 11, 19, 15, 30)),
        ("in 3 days", datetime(2026, 11, 22, 14, 30)),
        ("on 25 November", datetime(2026, 11, 25, 9, 0)),
        ("25th of Nov at 6pm", datetime(2026, 11, 25, 18, 0)),
        ("December 1", datetime(2026, 12, 1, 9, 0)),
        ("on 2 January", datetime(2027, 1, 2, 9, 0)),  # the next 2 January
        ("1/12", datetime(2026, 12, 1, 9, 0)),  # day/month
        ("2026-12-24 18:00", datetime(2026, 12, 24, 18, 0)),
        ("2026-12-24", datetime(2026, 12, 24, 9, 0)),
        ("next week", datetime(2026, 11, 23, 9, 0)),
    ],
)
def test_parses_the_owners_words(words: str, expected: datetime) -> None:
    assert parse_when(words, NOW) == expected


@pytest.mark.parametrize("words", ["", "someday", "when I get home", "31 February", "25:00"])
def test_unclear_words_are_refused_not_guessed(words: str) -> None:
    with pytest.raises(WhenError):
        parse_when(words, NOW)


def test_day_ranges() -> None:
    assert parse_day_range("all", NOW) is None
    assert parse_day_range("Friday", NOW) == (datetime(2026, 11, 20), datetime(2026, 11, 21))
    assert parse_day_range("today", NOW) == (datetime(2026, 11, 19), datetime(2026, 11, 20))
    assert parse_day_range("this week", NOW) == (datetime(2026, 11, 19), datetime(2026, 11, 23))
    assert parse_day_range("next week", NOW) == (datetime(2026, 11, 23), datetime(2026, 11, 30))
    with pytest.raises(WhenError):
        parse_day_range("soon-ish", NOW)


def test_describe() -> None:
    assert describe(datetime(2026, 11, 20, 9, 0)) == "Fri 20 Nov 2026, 09:00"
