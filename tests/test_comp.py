"""Comp extraction: annualizing structured bands, and reading pay out of prose."""

from __future__ import annotations

import pytest

from engine.comp import annualize, html_to_text, parse_salary_text


@pytest.mark.parametrize(
    "value, interval, expected, scaled",
    [
        (200000, "1 YEAR", 200000, False),
        (30, "1 HOUR", 62400, True),
        (100, "1 HOUR", 208000, True),
        (15000, "1 MONTH", 180000, True),
        (200000, None, 200000, False),
        (None, "1 YEAR", None, False),
    ],
)
def test_annualize(value, interval, expected, scaled):
    assert annualize(value, interval) == (expected, scaled)


def test_a_hundred_dollars_an_hour_clears_a_160k_floor():
    """The whole point of annualizing. Read naively, this role pays 100 a year."""
    annual, _ = annualize(100, "1 HOUR")
    assert annual > 160000


@pytest.mark.parametrize(
    "text, expected",
    [
        ("The base salary range for this role is $185,000 - $225,000.", (185000, 225000)),
        ("The annual OTE for this position is $90,000 - $120,000 USD.", (90000, 120000)),
        ("Salary: $160,000 to $230,000 per year", (160000, 230000)),
        ("Compensation is $30 - $38 per hour.", (62400, 79040)),
        ("The salary for this role is $185k.", (185000, 185000)),
    ],
)
def test_parse_salary_text_finds_the_band(text, expected):
    parsed = parse_salary_text(text)
    assert (parsed.min, parsed.max) == expected


@pytest.mark.parametrize(
    "text",
    [
        "We offer a $5,000 signing bonus and unlimited PTO.",
        "Employees receive a $500 monthly wellness stipend.",
        "We have raised $200,000,000 in Series C funding.",
        "",
        None,
    ],
)
def test_parse_salary_text_ignores_money_that_is_not_pay(text):
    """A job description is full of dollar signs. Most of them are not salary."""
    parsed = parse_salary_text(text)
    assert parsed.min is None and parsed.max is None


def test_ote_is_flagged_because_it_is_not_base_salary():
    parsed = parse_salary_text("The annual OTE for this position is $90,000 - $120,000 USD.")
    assert "comp-ote" in parsed.flags


def test_hourly_prose_is_flagged_as_annualized():
    parsed = parse_salary_text("Compensation is $30 - $38 per hour.")
    assert "comp-hourly-annualized" in parsed.flags


def test_a_lone_number_is_flagged_as_a_point_estimate():
    parsed = parse_salary_text("The salary for this role is $185k.")
    assert "comp-single-value" in parsed.flags


def test_html_to_text_survives_greenhouse_double_escaping():
    """Greenhouse escapes its HTML, and escapes the entities inside it again.

    One unescape pass leaves literal &nbsp; sitting in the description text.
    """
    raw = "&lt;p&gt;Base salary is $190,000.&amp;nbsp;&lt;/p&gt;&lt;p&gt;R&amp;amp;D team.&lt;/p&gt;"
    text = html_to_text(raw)

    assert "<p>" not in text
    assert "&nbsp;" not in text
    assert "&amp;" not in text
    assert "R&D team." in text
    assert parse_salary_text(text).min == 190000


def test_html_to_text_handles_nothing():
    assert html_to_text(None) is None
