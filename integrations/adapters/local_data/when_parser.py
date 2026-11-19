"""Turn the owner's time words ("on Friday", "tomorrow at 9", "in 20 minutes") into a local
date and time, in code (build step 15, Planner agent).

A 4B model is not reliable at calendar arithmetic, so the Planner's model copies the owner's
own words into `when` and this module works out the date. Anything it does not understand is
refused with a plain message (the agent then asks the owner) instead of guessed.

Rules:
- A day without a time means 09:00 (DEFAULT_HOUR).
- A time without a day means today, or tomorrow if that time has already passed.
- A weekday ("Friday", "on Friday", "next Friday") means the next such day after today.
- Numeric dates are day/month ("25/11"), as the owner writes them; a date without a year is
  the next time that date comes round.
"""

from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta

DEFAULT_HOUR = 9

WEEKDAYS = {
    "monday": 0, "mon": 0, "tuesday": 1, "tue": 1, "tues": 1, "wednesday": 2, "wed": 2,
    "thursday": 3, "thu": 3, "thur": 3, "thurs": 3, "friday": 4, "fri": 4,
    "saturday": 5, "sat": 5, "sunday": 6, "sun": 6,
}  # fmt: skip
MONTHS = {
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3, "april": 4,
    "apr": 4, "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7, "august": 8, "aug": 8,
    "september": 9, "sep": 9, "sept": 9, "october": 10, "oct": 10, "november": 11, "nov": 11,
    "december": 12, "dec": 12,
}  # fmt: skip
PARTS_OF_DAY = {"morning": 9, "noon": 12, "midday": 12, "afternoon": 15, "evening": 18}
PARTS_OF_DAY |= {"tonight": 20, "night": 20, "midnight": 0}

_MONTH = "|".join(sorted(MONTHS, key=len, reverse=True))
_DAY = "|".join(sorted(WEEKDAYS, key=len, reverse=True))
_RELATIVE = re.compile(r"\bin\s+(an?|\d{1,3})\s+(minute|min|hour|hr|day|week)s?\b")
_ISO = re.compile(r"\b(\d{4})-(\d{1,2})-(\d{1,2})(?:[t\s]+(\d{1,2}):(\d{2}))?\b")
_DAY_MONTH = re.compile(
    rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?({_MONTH})\b\.?(?:\s+(\d{{4}}))?"
)
_MONTH_DAY = re.compile(rf"\b({_MONTH})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?\b(?:,?\s+(\d{{4}}))?")
_NUMERIC = re.compile(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b")
_WEEKDAY = re.compile(rf"\b(?:(next|this|coming)\s+)?({_DAY})\b")
_CLOCK = re.compile(r"\b(\d{1,2})(?:[:.](\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)?(?=\W|$)")
_AT_TIME = re.compile(r"\b(?:at|by|@)\s+(\d{1,2})(?:[:.](\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)?(?=\W|$)")


class WhenError(ValueError):
    """The words are not a date/time this parser understands. Safe to show."""


def _clock(hour: int, minute: int, suffix: str | None) -> time:
    suffix = (suffix or "").replace(".", "")
    if suffix == "pm" and hour < 12:
        hour += 12
    elif suffix == "am" and hour == 12:
        hour = 0
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise WhenError("that time does not exist")
    return time(hour, minute)


def _find_time(text: str) -> tuple[time | None, str]:
    """The clock time in `text` (if any) and the text with it removed."""
    match = _AT_TIME.search(text)
    if match is None:
        # a bare time needs am/pm or minutes ("9pm", "17:30"), so "25 nov" is not a time
        for candidate in _CLOCK.finditer(text):
            if candidate.group(2) or candidate.group(3):
                match = candidate
                break
    if match is not None:
        found = _clock(int(match.group(1)), int(match.group(2) or 0), match.group(3))
        return found, text[: match.start()] + " " + text[match.end() :]
    for word, hour in PARTS_OF_DAY.items():
        if re.search(rf"\b{word}\b", text):
            return time(hour, 0), re.sub(rf"\b{word}\b", " ", text)
    return None, text


def _make_date(year: int | None, month: int, day: int, today: date) -> date:
    try:
        if year is not None:
            return date(year if year > 99 else 2000 + year, month, day)
        found = date(today.year, month, day)
        return found if found >= today else date(today.year + 1, month, day)
    except ValueError:
        raise WhenError("that date does not exist") from None


def _find_day(text: str, today: date) -> date | None:
    if re.search(r"\bday\s+after\s+tomorrow\b", text):
        return today + timedelta(days=2)
    if re.search(r"\btomorrow\b", text):
        return today + timedelta(days=1)
    if re.search(r"\b(today|tonight)\b", text):
        return today
    if m := _ISO.search(text):
        return _make_date(int(m.group(1)), int(m.group(2)), int(m.group(3)), today)
    if m := _DAY_MONTH.search(text):
        year = int(m.group(3)) if m.group(3) else None
        return _make_date(year, MONTHS[m.group(2)], int(m.group(1)), today)
    if m := _MONTH_DAY.search(text):
        year = int(m.group(3)) if m.group(3) else None
        return _make_date(year, MONTHS[m.group(1)], int(m.group(2)), today)
    if m := _NUMERIC.search(text):
        year = int(m.group(3)) if m.group(3) else None
        return _make_date(year, int(m.group(2)), int(m.group(1)), today)
    if m := _WEEKDAY.search(text):
        ahead = (WEEKDAYS[m.group(2)] - today.weekday()) % 7 or 7
        return today + timedelta(days=ahead)
    if re.search(r"\bnext\s+week\b", text):
        return today + timedelta(days=7 - today.weekday())  # next Monday
    return None


def parse_when(words: str, now: datetime) -> datetime:
    """The local date and time the owner's words mean. Raises WhenError if unclear."""
    text = " ".join(words.lower().split())
    if not text:
        raise WhenError("no date or time was given")
    if m := _RELATIVE.search(text):
        amount = 1 if m.group(1) in ("a", "an") else int(m.group(1))
        unit = m.group(2)
        delta = {
            "minute": timedelta(minutes=amount),
            "min": timedelta(minutes=amount),
            "hour": timedelta(hours=amount),
            "hr": timedelta(hours=amount),
            "day": timedelta(days=amount),
            "week": timedelta(weeks=amount),
        }[unit]
        return (now + delta).replace(second=0, microsecond=0)
    if m := _ISO.search(text):
        if m.group(4):
            day = _make_date(int(m.group(1)), int(m.group(2)), int(m.group(3)), now.date())
            return datetime.combine(day, _clock(int(m.group(4)), int(m.group(5)), None))
    clock, rest = _find_time(text)
    day = _find_day(rest, now.date())
    if day is None and clock is None:
        raise WhenError(f"I could not tell which day or time '{words.strip()}' means")
    if day is None and clock is not None:
        moment = datetime.combine(now.date(), clock)
        return moment if moment > now else moment + timedelta(days=1)
    return datetime.combine(day, clock or time(DEFAULT_HOUR, 0))


def parse_day_range(words: str, now: datetime) -> tuple[datetime, datetime] | None:
    """[start, end) for "today", "tomorrow", "this week", "next 7 days", a weekday or a date.
    None for "all" / empty (no limit). Raises WhenError if unclear."""
    text = " ".join(words.lower().split())
    if text in ("", "all", "everything", "any", "upcoming", "all upcoming"):
        return None
    start_of_today = datetime.combine(now.date(), time(0, 0))
    if re.search(r"\b(this|the)\s+week\b|\bweek\b", text) and "next" not in text:
        end = start_of_today + timedelta(days=7 - now.weekday())
        return start_of_today, end
    if m := re.search(r"\bnext\s+(\d{1,2})\s+days\b", text):
        return start_of_today, start_of_today + timedelta(days=int(m.group(1)) + 1)
    if re.search(r"\bnext\s+week\b", text):
        monday = start_of_today + timedelta(days=7 - now.weekday())
        return monday, monday + timedelta(days=7)
    day = _find_day(text, now.date())
    if day is None:
        raise WhenError(f"I could not tell which day '{words.strip()}' means")
    start = datetime.combine(day, time(0, 0))
    return start, start + timedelta(days=1)


def describe(moment: datetime) -> str:
    """'Fri 20 Nov 2026, 09:00' — how a due time is shown to the owner."""
    return f"{moment:%a} {moment.day} {moment:%b %Y, %H:%M}"
