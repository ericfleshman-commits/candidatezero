"""Greenhouse sourcer, against a recorded real board.

The expensive-last rule is the thing under test here. Greenhouse charges a
request per role to learn its pay, and most roles are not candidates.
"""

from __future__ import annotations

import httpx
import respx

from engine.config import CompanyEntry
from engine.sourcers.base import OrgNotFound
from engine.sourcers.greenhouse import DETAIL_URL, LIST_URL, GreenhouseSourcer

ORG = CompanyEntry(slug="gongio", company="Gong")


def _mock_list(payload: dict) -> None:
    respx.get(LIST_URL.format(slug=ORG.slug)).mock(return_value=httpx.Response(200, json=payload))


def _mock_detail(job_id: int, payload: dict):
    return respx.get(DETAIL_URL.format(slug=ORG.slug, job_id=job_id)).mock(
        return_value=httpx.Response(200, json=payload)
    )


@respx.mock
def test_pipe_separated_multi_location_is_preserved(greenhouse_list, client):
    _mock_list(greenhouse_list)
    roles = GreenhouseSourcer(client).fetch(ORG, needs_detail=lambda r: False)
    role = next(r for r in roles if r.title == "Commercial Revenue Architect")

    assert "|" in role.location_raw
    assert "New York City" in role.location_raw


@respx.mock
def test_list_response_carries_no_comp(greenhouse_list, client):
    """Greenhouse publishes no pay in the list endpoint. Not zero. Absent."""
    _mock_list(greenhouse_list)
    roles = GreenhouseSourcer(client).fetch(ORG, needs_detail=lambda r: False)

    assert all(r.comp.source == "none" for r in roles)


@respx.mock
def test_detail_is_fetched_only_for_survivors(greenhouse_list, greenhouse_detail, client):
    _mock_list(greenhouse_list)
    route = _mock_detail(4679941006, greenhouse_detail)

    sourcer = GreenhouseSourcer(client)
    wanted = "Commercial Revenue Architect"
    roles = sourcer.fetch(ORG, needs_detail=lambda r: r.title == wanted)

    # Three roles on the board, exactly one worth paying for.
    assert len(roles) == 3
    assert route.call_count == 1
    assert sourcer.detail_fetches == 1


@respx.mock
def test_detail_parses_the_band_out_of_escaped_html(greenhouse_list, greenhouse_detail, client):
    _mock_list(greenhouse_list)
    _mock_detail(4679941006, greenhouse_detail)

    roles = GreenhouseSourcer(client).fetch(
        ORG, needs_detail=lambda r: r.title == "Commercial Revenue Architect"
    )
    role = next(r for r in roles if r.title == "Commercial Revenue Architect")

    assert (role.comp.min, role.comp.max) == (90000, 120000)
    assert role.comp.source == "parsed"
    # The band came out of a sentence about OTE, which folds in commission. That
    # is not base salary and the digest must not imply it is.
    assert "comp-ote" in role.flags
    assert "<p>" not in (role.description_text or "")
    assert "&nbsp;" not in (role.description_text or "")


@respx.mock
def test_a_failed_detail_fetch_does_not_kill_the_role(greenhouse_list, client):
    _mock_list(greenhouse_list)
    respx.get(DETAIL_URL.format(slug=ORG.slug, job_id=4679941006)).mock(
        return_value=httpx.Response(500)
    )

    roles = GreenhouseSourcer(client).fetch(
        ORG, needs_detail=lambda r: r.title == "Commercial Revenue Architect"
    )
    role = next(r for r in roles if r.title == "Commercial Revenue Architect")

    assert role.comp.source == "none"
    assert "detail-fetch-failed" in role.flags


@respx.mock
def test_a_404_board_raises_orgnotfound(client):
    respx.get(LIST_URL.format(slug="chainalysis-careers")).mock(return_value=httpx.Response(404))

    try:
        GreenhouseSourcer(client).fetch(CompanyEntry(slug="chainalysis-careers"))
    except OrgNotFound:
        return
    raise AssertionError("a 404 board must raise OrgNotFound")


@respx.mock
def test_company_name_comes_from_greenhouse_not_our_guess(greenhouse_list, client):
    _mock_list(greenhouse_list)
    roles = GreenhouseSourcer(client).fetch(
        CompanyEntry(slug="gongio", company="Gong"), needs_detail=lambda r: False
    )
    assert roles[0].company_name == "Gong.io"
