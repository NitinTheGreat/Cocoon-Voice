"""Deterministic interpretation of when an incident happened, from the operator's own words.

The model (or the mock router) only extracts the phrase; this module decides what it means. The reference time is
persisted (the backend's first receipt of the turn or command), so a retry reinterprets against the same instant and
can never move the occurrence forward. The trusted site offset comes from the session's seeded shift.

Supported, documented set (anything else is `ambiguous` or `unsupported` and the operator is asked again):

| Expression | Meaning |
|---|---|
| "just now", "right now", "a moment ago", "moments ago" | the reference time |
| "N minutes ago", "N mins ago", "N hours ago", "N hrs ago" (N as digits or words one..sixty, "a"/"an" = 1) | reference - N units (at most 12 hours back) |
| "half an hour ago" | reference - 30 minutes |
| "at HH:MM" (24-hour, HH >= 13, or HH:MM with am/pm), "at H am/pm", "at H:MM am/pm" | that site-local time on the reference date; a later-than-now result is ambiguous |
| "at HH:MM" or "at H" with H <= 12 and no am/pm | resolved only when exactly one of the two readings is earlier the same site-local day |

Vague phrases ("earlier", "this morning", "a while ago", "a few minutes ago", "yesterday", "recently", ...) are
`ambiguous`: they name a time but not one precise enough to store. No phrase at all is `none`, which callers store as
an explicitly labelled time-of-report basis, never as a known occurrence time.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal

Status = Literal["resolved", "ambiguous", "unsupported", "none"]
Basis = Literal["operator_relative", "operator_clock_time"]

MAX_LOOKBACK = timedelta(hours=12)

_WORDS = {
    "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
    "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16,
    "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20, "twenty five": 25, "twenty-five": 25,
    "thirty": 30, "forty": 40, "forty five": 45, "forty-five": 45, "fifty": 50, "sixty": 60,
}
_NUM = r"(\d{1,3}|" + "|".join(sorted((re.escape(w) for w in _WORDS), key=len, reverse=True)) + r")"
_AGO = re.compile(rf"\b(?:about |around |roughly |maybe )?{_NUM}\s+(minutes?|mins?|hours?|hrs?)\s+ago\b", re.I)
_HALF_HOUR = re.compile(r"\bhalf an hour ago\b", re.I)
_NOW = re.compile(r"\b(just now|right now|a moment ago|moments ago)\b", re.I)
_CLOCK = re.compile(r"\bat\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)?(?=\W|$)", re.I)
_VAGUE = re.compile(r"\b(earlier( today)?|this morning|this afternoon|a while ago|a few (minutes|hours) ago|"
                    r"some time ago|sometime|recently|yesterday|last night|last shift|before lunch|after lunch|"
                    r"a couple of (minutes|hours) ago|an hour or so ago)\b", re.I)


@dataclass(frozen=True)
class TimeInterpretation:
    status: Status
    expression: str | None = None
    occurred_at: datetime | None = None
    basis: Basis | None = None
    reason: str | None = None  # why it is ambiguous/unsupported (operator-facing, short)


def find_expression(text: str) -> str | None:
    """The time phrase in an utterance, if any (used by the mock router; the live model fills the same field)."""
    for rx in (_HALF_HOUR, _AGO, _NOW, _CLOCK, _VAGUE):
        m = rx.search(text)
        if m:
            return m.group(0).strip()
    return None


def interpret(expression: str | None, reference: datetime, site_offset: timezone | None) -> TimeInterpretation:
    """Interpret one extracted phrase against a persisted, timezone-aware reference time."""
    if reference.tzinfo is None:
        raise ValueError("reference time must be timezone-aware")
    if not expression or not expression.strip():
        return TimeInterpretation(status="none")
    expr = expression.strip()
    if _NOW.search(expr):
        return TimeInterpretation("resolved", expr, reference, "operator_relative")
    if _HALF_HOUR.search(expr):
        return TimeInterpretation("resolved", expr, reference - timedelta(minutes=30), "operator_relative")
    m = _AGO.search(expr)
    if m:
        raw, unit = m.group(1).lower(), m.group(2).lower()
        n = int(raw) if raw.isdigit() else _WORDS[raw]
        delta = timedelta(hours=n) if unit.startswith("h") else timedelta(minutes=n)
        if n <= 0 or delta > MAX_LOOKBACK:
            return TimeInterpretation("unsupported", expr, reason="more than 12 hours ago")
        return TimeInterpretation("resolved", expr, reference - delta, "operator_relative")
    m = _CLOCK.search(expr)
    if m:
        if site_offset is None:
            return TimeInterpretation("unsupported", expr, reason="no trusted site time zone for a clock time")
        return _clock(expr, int(m.group(1)), int(m.group(2) or 0), (m.group(3) or "").lower(), reference,
                      site_offset)
    if _VAGUE.search(expr):
        return TimeInterpretation("ambiguous", expr, reason="not precise enough")
    return TimeInterpretation("unsupported", expr, reason="not a supported time expression")


def _clock(expr: str, hour: int, minute: int, meridiem: str, reference: datetime,
           tz: timezone) -> TimeInterpretation:
    if minute > 59 or hour > 23 or (meridiem and not 1 <= hour <= 12):
        return TimeInterpretation("unsupported", expr, reason="not a valid clock time")
    local_ref = reference.astimezone(tz)
    days: tuple[int, ...] = (0, -1)  # today at the site, or yesterday for a time just before midnight
    if meridiem:
        hours = [hour % 12 + (12 if meridiem.startswith("p") else 0)]
    elif hour >= 13 or hour == 0:
        hours = [hour]
    else:
        # A 12-hour reading without am/pm is only resolved within the same site-local day; reaching back to the
        # previous evening would be a guess.
        hours = [hour, hour + 12] if hour < 12 else [12, 0]
        days = (0,)
    candidates = []
    for h in hours:
        for day in days:
            at = (local_ref.replace(hour=h, minute=minute, second=0, microsecond=0) + timedelta(days=day))
            if at <= local_ref and local_ref - at <= MAX_LOOKBACK:
                candidates.append(at)
    candidates = sorted(set(candidates))
    if len(candidates) == 1:
        return TimeInterpretation("resolved", expr, candidates[0].astimezone(timezone.utc), "operator_clock_time")
    if not candidates:
        return TimeInterpretation("ambiguous", expr, reason="that time is later than now or more than 12 hours ago")
    return TimeInterpretation("ambiguous", expr, reason="morning or evening is unclear")


def offset_timezone(utc_offset: str | None) -> timezone | None:
    """'+05:30' -> timezone; None when the session has no trusted site."""
    if not utc_offset:
        return None
    sign = 1 if utc_offset[0] == "+" else -1
    return timezone(sign * timedelta(hours=int(utc_offset[1:3]), minutes=int(utc_offset[4:6])))
