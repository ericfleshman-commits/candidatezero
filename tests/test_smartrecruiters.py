"""SmartRecruiters sourcer against a recorded board.

tests/fixtures/smartrecruiters_postings.json is a real response from the
HireVue board, recorded 2026-07-26, trimmed to three postings. The detail
fixture is the first posting's jobAd. The shapes that matter: an explicit
location.remote boolean, releasedDate, no description and no comp anywhere
in the list, and pagination via totalFound.
"""

from __future__ import annotations

import httpx
import respx

from conftest import load_fixture
from engine.config import CompanyEntry
from engine.sourcers.smartrecruiters import (
    DETAIL_URL,
    LIST_URL,
    SmartRecruitersSourcer,
)

ORG = CompanyEntry(slug="hirevue", company="HireVue")


def _page_url(offset: int) -> str:
    return LIST_URL.format(slug="hirevue", limit=100, offset=offset)


def _mock_board() -> dict:
    fixture = load_fixture("smartrecruiters_postings.json")
    # The recorded board holds 5 postings and the trim kept 3. Truth the total
    # so the paginator does not ask for a page the mock never serves.
    fixture["totalFound"] = len(fixture["content"])
    respx.get(_page_url(0)).mock(return_value=httpx.Response(200, json=fixture))
    return fixture


@respx.mock
def test_fetch_normalizes_the_recorded_board(client):
    _mock_board()

    roles = SmartRecruitersSourcer(client).fetch(ORG, needs_detail=lambda r: False)

    assert len(roles) == 3
    by_title = {r.title: r for r in roles}

    intern = by_title["Data Science Intern | Fully Remote US"]
    assert intern.source == "smartrecruiters"
    assert intern.id.startswith("smartrecruiters:hirevue:")
    assert intern.company_name == "HireVue Inc"  # the API's legal name, not our guess
    assert intern.is_remote is True
    assert intern.location_raw == "Sandy, UT, United States"
    assert intern.url == "https://jobs.smartrecruiters.com/HireVue/744000138728139"
    assert intern.published_at is not None
    assert intern.comp.source == "none"  # the list publishes no band at all


@respx.mock
def test_pagination_walks_until_total_found(client):
    page = load_fixture("smartrecruiters_postings.json")
    first = dict(page, totalFound=5)
    second = dict(page, totalFound=5, content=page["content"][:2])
    respx.get(_page_url(0)).mock(return_value=httpx.Response(200, json=first))
    respx.get(_page_url(3)).mock(return_value=httpx.Response(200, json=second))

    roles = SmartRecruitersSourcer(client).fetch(ORG, needs_detail=lambda r: False)
    assert len(roles) == 5


@respx.mock
def test_an_empty_page_ends_the_walk_instead_of_looping(client):
    """totalFound lies for unknown and empty boards alike. Recorded 2026-07-25."""
    page = load_fixture("smartrecruiters_postings.json")
    first = dict(page, totalFound=50)
    respx.get(_page_url(0)).mock(return_value=httpx.Response(200, json=first))
    respx.get(_page_url(3)).mock(
        return_value=httpx.Response(200, json={"totalFound": 50, "content": []})
    )

    roles = SmartRecruitersSourcer(client).fetch(ORG, needs_detail=lambda r: False)
    assert len(roles) == 3


@respx.mock
def test_detail_is_gated_and_fills_url_and_description(client):
    _mock_board()
    detail = load_fixture("smartrecruiters_detail.json")
    respx.get(DETAIL_URL.format(slug="hirevue", posting_id="744000138728139")).mock(
        return_value=httpx.Response(200, json=detail)
    )

    sourcer = SmartRecruitersSourcer(client)
    roles = sourcer.fetch(ORG, needs_detail=lambda r: "Data Science" in r.title)

    assert sourcer.detail_fetches == 1
    enriched = next(r for r in roles if "Data Science" in r.title)
    assert enriched.url == detail["postingUrl"]
    assert enriched.description_text
    others = [r for r in roles if "Data Science" not in r.title]
    assert all(r.description_text is None for r in others)


@respx.mock
def test_a_band_in_the_job_ad_prose_is_parsed(client):
    _mock_board()
    detail = {
        "postingUrl": "https://jobs.smartrecruiters.com/HireVue/744000138728139-x",
        "jobAd": {
            "sections": {
                "jobDescription": {
                    "text": "<p>The base salary range for this role is $165,000 to $190,000.</p>"
                }
            }
        },
    }
    respx.get(DETAIL_URL.format(slug="hirevue", posting_id="744000138728139")).mock(
        return_value=httpx.Response(200, json=detail)
    )

    roles = SmartRecruitersSourcer(client).fetch(
        ORG, needs_detail=lambda r: "Data Science" in r.title
    )
    enriched = next(r for r in roles if "Data Science" in r.title)
    assert (enriched.comp.min, enriched.comp.max, enriched.comp.source) == (
        165000,
        190000,
        "parsed",
    )


@respx.mock
def test_a_failed_detail_fetch_flags_and_never_crashes(client):
    _mock_board()
    respx.get(DETAIL_URL.format(slug="hirevue", posting_id="744000138728139")).mock(
        return_value=httpx.Response(500)
    )

    roles = SmartRecruitersSourcer(client).fetch(
        ORG, needs_detail=lambda r: "Data Science" in r.title
    )
    enriched = next(r for r in roles if "Data Science" in r.title)
    assert "detail-fetch-failed" in enriched.flags
    assert enriched.comp.source == "none"
