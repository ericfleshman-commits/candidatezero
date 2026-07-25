"""Lever sourcer against a recorded board.

tests/fixtures/lever_postings.json is a real response from the Outreach board,
recorded 2026-07-25, trimmed to four postings and de-styled. It carries the
shapes that matter: a structured salaryRange, a posting with no band at all,
a remote US role, and a hybrid London one.
"""

from __future__ import annotations

import httpx
import respx

from conftest import load_fixture
from engine.config import CompanyEntry
from engine.models import CompRange
from engine.sourcers.lever import POSTINGS_URL, LeverSourcer, extract_comp

URL = POSTINGS_URL.format(slug="outreach")
ORG = CompanyEntry(slug="outreach", company="Outreach")


@respx.mock
def test_fetch_normalizes_the_recorded_board(client):
    respx.get(URL).mock(return_value=httpx.Response(200, json=load_fixture("lever_postings.json")))

    roles = LeverSourcer(client).fetch(ORG)

    assert len(roles) == 4
    by_title = {r.title: r for r in roles}

    gtm = by_title["AI GTM Architect (Revenue Operations)"]
    assert gtm.source == "lever"
    assert gtm.id.startswith("lever:outreach:")
    assert gtm.company_name == "Outreach"
    assert (gtm.comp.min, gtm.comp.max, gtm.comp.source) == (145000, 185000, "structured")
    assert gtm.is_remote is True
    assert "United States" in gtm.location_raw
    assert gtm.url.startswith("https://jobs.lever.co/outreach/")
    assert gtm.published_at is not None

    emea = by_title["Account Manager, Commercial, EMEA"]
    assert emea.comp.source in ("none", "parsed")  # no structured band on this one
    assert emea.is_remote is None
    assert "London" in emea.location_raw


@respx.mock
def test_an_empty_list_is_a_quiet_board_not_an_error(client):
    """A valid org with nothing published answers 200 and []. Recorded 2026-07-25."""
    respx.get(POSTINGS_URL.format(slug="plaid")).mock(return_value=httpx.Response(200, json=[]))

    roles = LeverSourcer(client).fetch(CompanyEntry(slug="plaid"))
    assert roles == []


def test_hourly_salary_range_is_annualized():
    comp, flags = extract_comp(
        {"salaryRange": {"min": 50, "max": 60, "currency": "USD", "interval": "per-hour-wage"}}
    )
    assert (comp.min, comp.max) == (104000, 124800)
    assert comp.source == "structured"
    assert "comp-hourly-annualized" in flags


def test_prose_band_falls_back_to_the_parser():
    comp, _ = extract_comp(
        {
            "salaryRange": {},
            "descriptionPlain": "About the role.",
            "additionalPlain": "The annual base salary range is $150,000 - $180,000.",
        }
    )
    assert (comp.min, comp.max, comp.source) == (150000, 180000, "parsed")


def test_comp_range_defaults_to_none():
    comp, flags = extract_comp({"descriptionPlain": "We pay competitively."})
    assert comp == CompRange(source="none")
    assert flags == []
