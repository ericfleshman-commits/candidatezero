"""End to end, with the network mocked.

The rule under test everywhere here: a dead org must never crash the run.
"""

from __future__ import annotations

import httpx
import respx

from engine.config import CompaniesConfig, CompanyEntry, Config
from engine.discover import SMARTRECRUITERS_API, WORKABLE_API
from engine.pipeline import check_org, run_pipeline
from engine.sourcers.ashby import BOARD_URL
from engine.sourcers.greenhouse import LIST_URL
from engine.sourcers.lever import POSTINGS_URL

ASHBY = BOARD_URL.format(slug="wealth-com")
GH = LIST_URL.format(slug="gongio")


def _mock_other_vendors_404(slug: str) -> None:
    """check-org probes every vendor; most tests only care about two of them."""
    respx.get(POSTINGS_URL.format(slug=slug)).mock(return_value=httpx.Response(404))
    respx.get(WORKABLE_API.format(slug=slug)).mock(return_value=httpx.Response(404))
    respx.get(SMARTRECRUITERS_API.format(slug=slug)).mock(
        return_value=httpx.Response(200, json={"totalFound": 0, "content": []})
    )


def _ashby_only(filters_cfg) -> Config:
    return Config(
        companies=CompaniesConfig(ashby=[CompanyEntry(slug="wealth-com", company="Wealth.com")]),
        filters=filters_cfg,
    )


@respx.mock
def test_run_produces_a_digest_worth_of_result(ashby_board, filters_cfg, client, tmp_path):
    respx.get(ASHBY).mock(return_value=httpx.Response(200, json=ashby_board))
    respx.head(url__regex=r"https://jobs\.ashbyhq\.com/.*").mock(
        return_value=httpx.Response(200)
    )

    result = run_pipeline(_ashby_only(filters_cfg), client, root=tmp_path, use_state=False)

    assert result.orgs_scanned == 1
    assert result.roles_seen == 4  # five in the fixture, one unlisted
    surviving = result.kept + result.flagged
    titles = {r.title for r in surviving}
    # Phoenix GTM Engineer is remote with a 130k to 180k band: kept, band-top-only.
    assert "GTM Engineer" in titles
    # Revenue Systems Engineer is NYC with no band: kept, comp-unknown.
    assert "Revenue Systems Engineer" in titles
    # Technical Support Associate misses on title.
    assert "Technical Support Associate" not in titles
    assert result.drop_counts.get("title", 0) >= 1


@respx.mock
def test_a_404_org_becomes_a_warning_not_a_crash(ashby_board, filters_cfg, client, tmp_path):
    cfg = Config(
        companies=CompaniesConfig(
            ashby=[
                CompanyEntry(slug="wealth-com", company="Wealth.com"),
                CompanyEntry(slug="totally-gone", company="Gone"),
            ]
        ),
        filters=filters_cfg,
    )
    respx.get(ASHBY).mock(return_value=httpx.Response(200, json=ashby_board))
    respx.get(BOARD_URL.format(slug="totally-gone")).mock(return_value=httpx.Response(404))
    respx.head(url__regex=r"https://jobs\.ashbyhq\.com/.*").mock(return_value=httpx.Response(200))

    result = run_pipeline(cfg, client, root=tmp_path, use_state=False)

    assert result.orgs_scanned == 1
    assert len(result.warnings) == 1
    assert result.warnings[0].slug == "totally-gone"
    assert "404" in result.warnings[0].reason
    assert result.kept or result.flagged  # the healthy org still produced roles


@respx.mock
def test_a_timing_out_org_becomes_a_warning_not_a_crash(filters_cfg, client, tmp_path):
    respx.get(ASHBY).mock(side_effect=httpx.ConnectTimeout("boom"))

    result = run_pipeline(_ashby_only(filters_cfg), client, root=tmp_path, use_state=False)

    assert result.orgs_scanned == 0
    assert len(result.warnings) == 1
    assert "unavailable" in result.warnings[0].reason


@respx.mock
def test_zombies_are_dropped_and_counted(ashby_board, filters_cfg, client, tmp_path):
    respx.get(ASHBY).mock(return_value=httpx.Response(200, json=ashby_board))
    respx.head(url__regex=r"https://jobs\.ashbyhq\.com/.*").mock(return_value=httpx.Response(404))
    respx.get(url__regex=r"https://jobs\.ashbyhq\.com/.*").mock(return_value=httpx.Response(404))

    result = run_pipeline(_ashby_only(filters_cfg), client, root=tmp_path, use_state=False)

    assert result.kept == [] and result.flagged == []
    assert result.drop_counts.get("zombie", 0) >= 1


@respx.mock
def test_second_run_reports_nothing_new(ashby_board, filters_cfg, client, tmp_path):
    """A nightly digest is a diff, not a reprint of the same roles forever."""
    respx.get(ASHBY).mock(return_value=httpx.Response(200, json=ashby_board))
    respx.head(url__regex=r"https://jobs\.ashbyhq\.com/.*").mock(return_value=httpx.Response(200))

    first = run_pipeline(_ashby_only(filters_cfg), client, root=tmp_path, use_state=True)
    assert first.kept or first.flagged

    second = run_pipeline(_ashby_only(filters_cfg), client, root=tmp_path, use_state=True)
    assert second.kept == [] and second.flagged == []
    assert second.still_open == len(first.kept) + len(first.flagged)


@respx.mock
def test_include_open_reprints_known_roles(ashby_board, filters_cfg, client, tmp_path):
    respx.get(ASHBY).mock(return_value=httpx.Response(200, json=ashby_board))
    respx.head(url__regex=r"https://jobs\.ashbyhq\.com/.*").mock(return_value=httpx.Response(200))

    first = run_pipeline(_ashby_only(filters_cfg), client, root=tmp_path, use_state=True)
    again = run_pipeline(
        _ashby_only(filters_cfg), client, root=tmp_path, use_state=True, include_open=True
    )

    assert len(again.kept) + len(again.flagged) == len(first.kept) + len(first.flagged)


@respx.mock
def test_check_org_finds_the_board_on_the_right_ats(ashby_board, client):
    respx.get(BOARD_URL.format(slug="wealth-com")).mock(
        return_value=httpx.Response(200, json=ashby_board)
    )
    respx.get(LIST_URL.format(slug="wealth-com")).mock(return_value=httpx.Response(404))
    _mock_other_vendors_404("wealth-com")

    findings = {f["source"]: f for f in check_org("wealth-com", client)}

    assert findings["ashby"]["status"] == "ok"
    assert findings["ashby"]["count"] == 4  # unlisted excluded
    assert findings["greenhouse"]["status"] == "404"
    assert findings["lever"]["status"] == "404"
    assert "workday" not in findings  # a bare slug cannot address a workday board


@respx.mock
def test_check_org_reports_a_slug_that_exists_nowhere(client):
    respx.get(BOARD_URL.format(slug="nope")).mock(return_value=httpx.Response(404))
    respx.get(LIST_URL.format(slug="nope")).mock(return_value=httpx.Response(404))
    _mock_other_vendors_404("nope")

    findings = check_org("nope", client)
    assert len(findings) == 5
    assert all(f["status"] == "404" for f in findings)
