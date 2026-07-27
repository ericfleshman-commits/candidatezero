"""Workable sourcer against a recorded board.

tests/fixtures/workable_account.json is a real response from the CrewAI board
(widget accounts API with details=true), recorded 2026-07-26, trimmed to four
postings and de-styled. It carries the shapes that matter: a remote US role, a
remote non-US role, an onsite San Francisco role, and inline HTML descriptions,
because details=true means Workable never needs a detail fetch.
"""

from __future__ import annotations

import httpx
import respx

from conftest import load_fixture
from engine.config import CompanyEntry
from engine.sourcers.workable import ACCOUNT_URL, WorkableSourcer

URL = ACCOUNT_URL.format(slug="crewai")
ORG = CompanyEntry(slug="crewai", company="CrewAI")


@respx.mock
def test_fetch_normalizes_the_recorded_board(client):
    respx.get(URL).mock(
        return_value=httpx.Response(200, json=load_fixture("workable_account.json"))
    )

    roles = WorkableSourcer(client).fetch(ORG)

    assert len(roles) == 4
    by_title = {r.title: r for r in roles}

    fde = by_title["Forward Deployed Engineer - (East coast based)"]
    assert fde.source == "workable"
    assert fde.id.startswith("workable:crewai:")
    assert fde.company_name == "CrewAI"  # the account's own name, not our guess
    assert fde.is_remote is True
    assert "United States" in fde.location_raw
    assert fde.url.startswith("https://apply.workable.com/")
    assert fde.published_at is not None
    assert fde.description_text  # details=true carries the JD inline

    onsite = by_title["Full-Stack Engineer, Agent Management Platform"]
    assert onsite.is_remote is False
    assert "San Francisco" in onsite.location_raw

    latam = by_title["Customer Success Manager, LATAM"]
    assert latam.is_remote is True
    assert "Brazil" in latam.location_raw


@respx.mock
def test_no_published_band_means_comp_source_none(client):
    """The public payload has no structured comp field at all. Recorded 2026-07-26."""
    respx.get(URL).mock(
        return_value=httpx.Response(200, json=load_fixture("workable_account.json"))
    )

    roles = WorkableSourcer(client).fetch(ORG)
    assert all(r.comp.source == "none" for r in roles)


@respx.mock
def test_a_band_in_the_description_prose_is_parsed(client):
    board = {
        "name": "Acme",
        "jobs": [
            {
                "title": "GTM Engineer",
                "shortcode": "ABC123",
                "telecommuting": True,
                "url": "https://apply.workable.com/j/ABC123",
                "published_on": "2026-07-01",
                "locations": [{"city": "New York", "region": "NY", "country": "United States"}],
                "description": "<p>The annual base salary range is $170,000 - $195,000.</p>",
            }
        ],
    }
    respx.get(URL).mock(return_value=httpx.Response(200, json=board))

    role = WorkableSourcer(client).fetch(ORG)[0]
    assert (role.comp.min, role.comp.max, role.comp.source) == (170000, 195000, "parsed")
    assert role.location_raw == "New York, NY, United States"
