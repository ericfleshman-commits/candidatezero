"""Turning published pay into a comparable number.

Two jobs live here:

1. Annualizing. Ashby publishes a structured band with an interval attached, and
   that interval is not always a year. A live board on 2026-07-16 carried both
   "1 YEAR" (614 components) and "1 HOUR" (8). Reading 30 and 38 off an hourly
   component and comparing them to a 160000 floor is how you silently throw away
   a real role, or keep a fake one.

2. Parsing prose. Greenhouse publishes no band in its API, so the number has to
   come out of the job description with a regex. That is an inference, not a
   fact, and it is recorded as comp_source "parsed" so the digest can say so.

The parser is deliberately keyword-gated. A job description is full of dollar
signs that are not salary: signing bonuses, equity, stipends, 401k matches.
Numbers are only trusted when they sit in a sentence that is talking about pay.
"""

from __future__ import annotations

import re

from pydantic import BaseModel

# Hours in a working year, the standard 40x52 convention.
HOURS_PER_YEAR = 2080

INTERVAL_MULTIPLIER: dict[str, int] = {
    "1 YEAR": 1,
    "1 MONTH": 12,
    "1 WEEK": 52,
    "1 DAY": 260,
    "1 HOUR": HOURS_PER_YEAR,
}

# A sentence must mention one of these before we believe its dollar signs.
SALARY_KEYWORDS = (
    "salary",
    "base pay",
    "base compensation",
    "compensation",
    "pay range",
    "pay band",
    "ote",
    "on-target",
    "on target",
    "annual",
    "per hour",
    "hourly",
    "per year",
)

OTE_KEYWORDS = ("ote", "on-target", "on target")
HOURLY_KEYWORDS = ("per hour", "/hour", "/hr", "hourly", "an hour")

# Matches "$185,000", "$185k", "$30". The optional k suffix is common in prose.
_MONEY = re.compile(r"\$\s*(\d[\d,]*(?:\.\d+)?)\s*([kK])?")
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+")


class ParsedComp(BaseModel):
    min: int | None = None
    max: int | None = None
    currency: str | None = None
    flags: list[str] = []


def annualize(value: int | float | None, interval: str | None) -> tuple[int | None, bool]:
    """Scale a pay figure to a yearly number.

    Returns the value and whether any scaling actually happened, so the caller can
    flag a band that was derived rather than published as annual.
    """
    if value is None:
        return None, False
    key = (interval or "").strip().upper()
    multiplier = INTERVAL_MULTIPLIER.get(key)
    if multiplier is None:
        # Unknown or absent interval. Assume the published number is annual, which
        # is what every board we have seen means when it does not say otherwise.
        return int(round(value)), False
    return int(round(value * multiplier)), multiplier != 1


def _to_int(raw: str, k_suffix: str | None) -> float:
    value = float(raw.replace(",", ""))
    if k_suffix and value < 1000:
        value *= 1000
    return value


def parse_salary_text(text: str | None) -> ParsedComp:
    """Pull a band out of prose. Returns an empty ParsedComp when nothing is credible."""
    if not text:
        return ParsedComp()

    normalized = re.sub(r"[ \t\xa0]+", " ", text)
    for segment in _SENTENCE_SPLIT.split(normalized):
        low = segment.lower()
        if not any(k in low for k in SALARY_KEYWORDS):
            continue

        matches = _MONEY.findall(segment)
        if not matches:
            continue

        hourly = any(k in low for k in HOURLY_KEYWORDS)
        values: list[int] = []
        for raw, suffix in matches:
            value = _to_int(raw, suffix)
            if hourly:
                annual, _ = annualize(value, "1 HOUR")
                if annual is not None:
                    values.append(annual)
            elif value >= 1000:
                # Below 1000 and not hourly is a stipend or a typo, not a salary.
                values.append(int(round(value)))

        if not values:
            continue

        values.sort()
        flags: list[str] = []
        if hourly:
            flags.append("comp-hourly-annualized")
        if any(k in low for k in OTE_KEYWORDS):
            # OTE folds commission into the number. For a GTM role that is a
            # materially different promise from base salary. Say so.
            flags.append("comp-ote")

        currency_match = re.search(r"\b(USD|CAD|EUR|GBP)\b", segment, re.IGNORECASE)
        currency = currency_match.group(1).upper() if currency_match else "USD"

        if len(values) == 1:
            flags.append("comp-single-value")
            return ParsedComp(min=values[0], max=values[0], currency=currency, flags=flags)
        return ParsedComp(min=values[0], max=values[-1], currency=currency, flags=flags)

    return ParsedComp()


def html_to_text(raw: str | None) -> str | None:
    """Greenhouse content is escaped HTML, and its entities are escaped twice.

    Verified against a live Gong posting on 2026-07-16: the payload carries
    "&lt;p&gt;" for tags and "&amp;nbsp;" for entities. So the correct decode is
    unescape, strip tags, then unescape again to resolve what the tags were
    hiding. One pass leaves "&nbsp;" sitting in the text.
    """
    if not raw:
        return None
    import html

    unescaped = html.unescape(raw)
    without_tags = re.sub(r"<[^>]+>", " ", unescaped)
    text = html.unescape(without_tags)
    text = text.replace("\xa0", " ")
    return re.sub(r"[ \t]+", " ", text).strip()
