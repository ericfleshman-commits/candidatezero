"""Ashby sourcer, against a recorded real board."""

from __future__ import annotations

import httpx
import respx

from engine.config import CompanyEntry
from engine.sourcers.ashby import BOARD_URL, AshbySourcer, extract_comp

ORG = CompanyEntry(slug="wealth-com", company="Wealth.com")


def _mock_board(payload: dict) -> None:
    respx.get(BOARD_URL.format(slug=ORG.slug)).mock(return_value=httpx.Response(200, json=payload))


@respx.mock
def test_unlisted_jobs_are_never_surfaced(ashby_board, client):
    _mock_board(ashby_board)
    roles = AshbySourcer(client).fetch(ORG)

    titles = [r.title for r in roles]
    assert "GTM Engineer (Draft, Unlisted)" not in titles
    # The fixture carries five jobs, one of which is a draft.
    assert len(roles) == 4


@respx.mock
def test_structured_annual_band_is_read_verbatim(ashby_board, client):
    _mock_board(ashby_board)
    roles = AshbySourcer(client).fetch(ORG)
    role = next(r for r in roles if r.title == "GTM Engineer")

    assert role.comp.source == "structured"
    assert (role.comp.min, role.comp.max) == (130000, 180000)
    assert role.comp.currency == "USD"
    assert "comp-hourly-annualized" not in role.flags


@respx.mock
def test_hourly_band_is_annualized_before_it_meets_the_floor(ashby_board, client):
    """The bug this catches is silent and expensive.

    Ashby publishes 30 and 38 on an hourly component. Compared naively to a
    160000 floor, an hourly role looks identical to a badly paid salaried one,
    and a 100 dollar an hour contract would be thrown away for looking like 100
    dollars a year.
    """
    _mock_board(ashby_board)
    roles = AshbySourcer(client).fetch(ORG)
    role = next(r for r in roles if r.title == "Technical Support Associate")

    assert (role.comp.min, role.comp.max) == (30 * 2080, 38 * 2080)
    assert role.comp.source == "structured"
    assert "comp-hourly-annualized" in role.flags


@respx.mock
def test_missing_compensation_block_is_none_not_zero(ashby_board, client):
    _mock_board(ashby_board)
    roles = AshbySourcer(client).fetch(ORG)
    role = next(r for r in roles if r.title == "Revenue Systems Engineer")

    assert role.comp.source == "none"
    assert role.comp.known is False
    assert role.comp.min is None and role.comp.max is None


@respx.mock
def test_secondary_locations_are_flattened_pipe_separated(ashby_board, client):
    """Matching Greenhouse's format means the location filter has one shape to parse."""
    _mock_board(ashby_board)
    roles = AshbySourcer(client).fetch(ORG)
    role = next(r for r in roles if r.title == "Software Engineer, Developer Productivity")

    assert role.location_raw == "San Francisco | New York City | Seattle"


@respx.mock
def test_role_id_is_namespaced_and_url_is_the_posting(ashby_board, client):
    _mock_board(ashby_board)
    roles = AshbySourcer(client).fetch(ORG)
    role = next(r for r in roles if r.title == "GTM Engineer")

    assert role.id.startswith("ashby:wealth-com:")
    assert role.url.startswith("https://jobs.ashbyhq.com/wealth-com/")
    assert role.published_at is not None
    assert role.description_text


def test_extract_comp_prefers_salary_over_equity_and_commission():
    """A tier can lead with equity. The salary component is the one we want."""
    job = {
        "compensation": {
            "compensationTiers": [
                {
                    "components": [
                        {"compensationType": "EquityPercentage", "minValue": 0.1, "maxValue": 0.5},
                        {"compensationType": "Commission", "minValue": 50000, "maxValue": 60000},
                        {
                            "compensationType": "Salary",
                            "interval": "1 YEAR",
                            "currencyCode": "USD",
                            "minValue": 190000,
                            "maxValue": 240000,
                        },
                    ]
                }
            ]
        }
    }
    comp, flags = extract_comp(job)
    assert (comp.min, comp.max, comp.source) == (190000, 240000, "structured")
    assert flags == []


def test_extract_comp_falls_back_to_the_prose_summary():
    job = {
        "compensation": {
            "compensationTiers": [],
            "scrapeableCompensationSalarySummary": "The salary range is $185,000 - $225,000 USD",
        }
    }
    comp, _ = extract_comp(job)
    assert (comp.min, comp.max, comp.source) == (185000, 225000, "parsed")
