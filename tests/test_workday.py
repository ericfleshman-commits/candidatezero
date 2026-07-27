"""Workday sourcer against a recorded board.

tests/fixtures/workday_jobs.json is a real cxs response from the Workday
tenant's own board, recorded 2026-07-26, trimmed to four postings. The detail
fixture is the Sr. SRE posting's jobPostingInfo, trimmed but keeping the
pay-transparency band, because that band living in JD prose is exactly the
fact the sourcer has to handle.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from conftest import load_fixture
from engine.config import CompanyEntry
from engine.sourcers.base import OrgNotFound
from engine.sourcers.workday import (
    DETAIL_URL,
    JOBS_URL,
    WorkdaySourcer,
    split_slug,
)

ORG = CompanyEntry(slug="workday.wd5/Workday", company="Workday")
LIST = JOBS_URL.format(tenant="workday", n="5", site="Workday")
DETAIL_PATH = "/job/USAVAReston/Sr-Site-Reliability-Engineer--US-Federal-_JR-0105486"

EMPTY_PAGE = {"total": 347, "jobPostings": []}


def _mock_board() -> None:
    respx.post(LIST).mock(
        side_effect=[
            httpx.Response(200, json=load_fixture("workday_jobs.json")),
            httpx.Response(200, json=EMPTY_PAGE),
        ]
    )


def test_split_slug_reads_tenant_wd_and_site():
    assert split_slug("workday.wd5/Workday") == ("workday", "5", "Workday")
    assert split_slug("iqvia.wd1/IQVIA") == ("iqvia", "1", "IQVIA")


def test_split_slug_rejects_a_bare_slug():
    """A bare slug cannot address a Workday board at all: 404-class, not a crash."""
    with pytest.raises(OrgNotFound):
        split_slug("gongio")


@respx.mock
def test_fetch_normalizes_the_recorded_board(client):
    _mock_board()

    roles = WorkdaySourcer(client).fetch(ORG, needs_detail=lambda r: False)

    assert len(roles) == 4
    by_title = {r.title: r for r in roles}

    sre = by_title["Sr. Site Reliability Engineer (US Federal)"]
    assert sre.source == "workday"
    assert sre.id == f"workday:workday.wd5/Workday:{DETAIL_PATH}"
    assert sre.company_name == "Workday"
    assert sre.location_raw == "USA.VA.Reston"
    assert sre.is_remote is False  # remoteType Onsite
    assert sre.url == f"https://workday.wd5.myworkdayjobs.com/Workday{DETAIL_PATH}"
    assert sre.published_at is None  # the list only says "Posted Yesterday"
    assert sre.comp.source == "none"

    flex = by_title["Analytics Sr Software Engineer (US Federal)"]
    assert flex.is_remote is None  # remoteType Flex is neither claim


@respx.mock
def test_an_empty_page_ends_the_walk_instead_of_looping(client):
    """The recorded board claims total 347; the mock serves one page. The walk
    must stop at the empty page, not chase the total forever."""
    _mock_board()
    roles = WorkdaySourcer(client).fetch(ORG, needs_detail=lambda r: False)
    assert len(roles) == 4


@respx.mock
def test_detail_is_gated_and_reads_date_band_and_url(client):
    _mock_board()
    detail = load_fixture("workday_detail.json")
    respx.get(DETAIL_URL.format(tenant="workday", n="5", site="Workday", path=DETAIL_PATH)).mock(
        return_value=httpx.Response(200, json=detail)
    )

    sourcer = WorkdaySourcer(client)
    roles = sourcer.fetch(ORG, needs_detail=lambda r: r.title.startswith("Sr."))

    assert sourcer.detail_fetches == 1
    enriched = next(r for r in roles if r.title.startswith("Sr."))
    assert enriched.published_at is not None
    assert enriched.published_at.isoformat().startswith("2026-07-25")
    assert enriched.url == detail["jobPostingInfo"]["externalUrl"]
    assert enriched.description_text
    # The pay-transparency block lists a primary and an additional-locations
    # band in one breath; the parser's verdict is the widest read, as an
    # inference, and says so.
    assert enriched.comp.source == "parsed"
    assert (enriched.comp.min, enriched.comp.max) == (137100, 243600)


@respx.mock
def test_a_failed_detail_fetch_flags_and_never_crashes(client):
    _mock_board()
    respx.get(DETAIL_URL.format(tenant="workday", n="5", site="Workday", path=DETAIL_PATH)).mock(
        return_value=httpx.Response(500)
    )

    roles = WorkdaySourcer(client).fetch(ORG, needs_detail=lambda r: r.title.startswith("Sr."))
    enriched = next(r for r in roles if r.title.startswith("Sr."))
    assert "detail-fetch-failed" in enriched.flags
    assert enriched.comp.source == "none"
