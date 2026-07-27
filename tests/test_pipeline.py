"""End to end, with the network mocked.

The rule under test everywhere here: a dead org must never crash the run.
"""

from __future__ import annotations

import json
from datetime import date

import httpx
import respx

from engine.config import CompaniesConfig, CompanyEntry, Config
from engine.discover import SMARTRECRUITERS_API, WORKABLE_API
from engine.pipeline import append_run_record, check_org, gather_orgs, run_pipeline
from engine.registry import OrgRecord, Registry
from engine.sourcers.ashby import BOARD_URL
from engine.sourcers.greenhouse import LIST_URL
from engine.sourcers.lever import POSTINGS_URL
from engine.sourcers.workday import JOBS_URL as WORKDAY_JOBS

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
    respx.head(url__regex=r"https://jobs\.ashbyhq\.com/.*").mock(return_value=httpx.Response(200))

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


def _live_registry(tmp_path, *records: OrgRecord) -> Registry:
    reg = Registry(tmp_path / "registry.jsonl")
    for record in records:
        reg.upsert(record)
        reg.mark_live(record.vendor, record.slug, 1, when=date(2026, 7, 25))
    return reg


@respx.mock
def test_registry_orgs_are_scanned_alongside_the_pins(ashby_board, filters_cfg, client, tmp_path):
    """The 8-org ceiling breaks here: live registry orgs join the night's scan."""
    reg = _live_registry(
        tmp_path,
        OrgRecord(vendor="lever", slug="outreach", company_name="Outreach"),
        OrgRecord(vendor="ashby", slug="wealth-com"),  # also pinned; must not scan twice
        # Harvested since sprint 19: a live workday org is scanned, not skipped.
        OrgRecord(vendor="workday", slug="acme.wd1/Ext", company_name="Acme"),
    )
    respx.get(ASHBY).mock(return_value=httpx.Response(200, json=ashby_board))
    respx.post(WORKDAY_JOBS.format(tenant="acme", n="1", site="Ext")).mock(
        return_value=httpx.Response(200, json={"total": 0, "jobPostings": []})
    )
    lever_route = respx.get(POSTINGS_URL.format(slug="outreach")).mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "id": "abc",
                    "text": "GTM Engineer",
                    "workplaceType": "remote",
                    "categories": {"location": "United States"},
                    "salaryRange": {"min": 170000, "max": 200000, "currency": "USD"},
                    "hostedUrl": "https://jobs.lever.co/outreach/abc",
                }
            ],
        )
    )
    respx.head(url__regex=r"https://jobs\.(ashbyhq|lever)\.co.*").mock(
        return_value=httpx.Response(200)
    )
    respx.head(url__regex=r"https://jobs\.ashbyhq\.com/.*").mock(return_value=httpx.Response(200))

    result = run_pipeline(
        _ashby_only(filters_cfg), client, root=tmp_path, use_state=False, registry=reg
    )

    assert result.orgs_scanned == 3  # wealth-com once, outreach once, the quiet workday board
    assert result.orgs_live == 3
    assert result.orgs_without_harvester == 0
    assert lever_route.call_count == 1
    assert "Outreach" in {r.company_name for r in result.kept + result.flagged}


def test_gather_orgs_pins_always_win(filters_cfg, tmp_path):
    cfg = _ashby_only(filters_cfg)
    reg = _live_registry(tmp_path, OrgRecord(vendor="ashby", slug="wealth-com", company_name="W"))

    orgs, without = gather_orgs(cfg, reg)

    assert [(s, e.slug) for s, e in orgs] == [("ashby", "wealth-com")]
    assert without == 0


def test_append_run_record_writes_one_json_line(tmp_path):
    from engine.models import RunResult

    result = RunResult(
        orgs_scanned=42,
        orgs_live=40,
        roles_seen=943,
        drop_counts={"title": 900, "location": 20, "comp": 10, "zombie": 3},
        still_open=4,
        duration_seconds=61.25,
    )

    path = append_run_record(result, root=tmp_path, run_date=date(2026, 7, 25))
    path = append_run_record(result, root=tmp_path, run_date=date(2026, 7, 26))

    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    record = json.loads(lines[0])
    assert record == {
        "date": "2026-07-25",
        "orgs_live": 40,
        "orgs_scanned": 42,
        "postings_read": 943,
        "eliminated": {"title": 900, "location": 20, "comp": 10, "liveness": 3},
        "survivors": 4,
        "flagged": 0,
        "suppressed": 0,
        "zombies": 3,
        "dead_orgs": 0,
        "duration_seconds": 61.2,
    }


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


@respx.mock
def test_a_ruled_out_role_is_suppressed_never_kept(ashby_board, filters_cfg, client, tmp_path):
    """Exhibit A. A dq'd company's roles land in suppressed, not in PASS or FLAG,
    and the engine does not spend a liveness probe on them."""
    from engine.dedupe import HistoryConfig, HistoryEntry

    respx.get(ASHBY).mock(return_value=httpx.Response(200, json=ashby_board))
    head_route = respx.head(url__regex=r"https://jobs\.ashbyhq\.com/.*").mock(
        return_value=httpx.Response(200)
    )
    history = HistoryConfig(
        entries=[
            HistoryEntry(
                company="Wealth.com",
                status="dq",
                role="GTM Engineer",
                date="2026-07-16",
                reason="software engineer seat in costume",
            )
        ]
    )

    result = run_pipeline(
        _ashby_only(filters_cfg), client, root=tmp_path, use_state=False, history=history
    )

    surviving_companies = {r.company_name for r in result.kept + result.flagged}
    assert "Wealth.com" not in surviving_companies
    suppressed_titles = {s.title for s in result.suppressed}
    assert "GTM Engineer" in suppressed_titles
    first = result.suppressed[0]
    assert first.status == "dq" and first.reason == "software engineer seat in costume"
    assert head_route.call_count == 0  # every survivor was suppressed before verify


@respx.mock
def test_role_level_history_leaves_other_roles_live_but_flagged(
    ashby_board, filters_cfg, client, tmp_path
):
    from engine.dedupe import HistoryConfig, HistoryEntry

    respx.get(ASHBY).mock(return_value=httpx.Response(200, json=ashby_board))
    respx.head(url__regex=r"https://jobs\.ashbyhq\.com/.*").mock(return_value=httpx.Response(200))
    # The fixture board has both a GTM Engineer and a Revenue Systems Engineer.
    history = HistoryConfig(
        entries=[
            HistoryEntry(
                company="Wealth.com", status="applied", role="GTM Engineer", date="2026-07-01"
            )
        ]
    )

    result = run_pipeline(
        _ashby_only(filters_cfg), client, root=tmp_path, use_state=False, history=history
    )

    assert {s.title for s in result.suppressed} == {"GTM Engineer"}
    live = {r.title: r for r in result.kept + result.flagged}
    assert "Revenue Systems Engineer" in live
    other = live["Revenue Systems Engineer"]
    assert "history-at-company" in other.flags
    assert any("history at this company: applied" in r for r in other.verdict.reasons)


@respx.mock
def test_suppression_holds_on_every_run_not_just_the_first(
    ashby_board, filters_cfg, client, tmp_path
):
    """A standing ruling has no expiry: a suppressed role is reported every
    night, even after the seen-store knows it, and never leaks into still_open."""
    from engine.dedupe import HistoryConfig, HistoryEntry

    respx.get(ASHBY).mock(return_value=httpx.Response(200, json=ashby_board))
    respx.head(url__regex=r"https://jobs\.ashbyhq\.com/.*").mock(return_value=httpx.Response(200))
    history = HistoryConfig(entries=[HistoryEntry(company="Wealth.com", status="blacklist")])

    first = run_pipeline(
        _ashby_only(filters_cfg), client, root=tmp_path, use_state=True, history=history
    )
    second = run_pipeline(
        _ashby_only(filters_cfg), client, root=tmp_path, use_state=True, history=history
    )

    assert first.suppressed and len(second.suppressed) == len(first.suppressed)
    assert second.kept == [] and second.flagged == [] and second.still_open == 0


@respx.mock
def test_public_store_is_fed_before_any_private_filtering(
    ashby_board, filters_cfg, client, tmp_path
):
    """The board's product decision, enforced at the pipeline seam.

    A history ruling suppresses Wealth.com's roles from the private digest, but
    the public store must still hold them: its only editorial rule is the title
    family. What it must NOT hold is the off-family role, or one byte of the
    private machinery (flags, statuses, reasons).
    """
    from engine.dedupe import HistoryConfig, HistoryEntry

    respx.get(ASHBY).mock(return_value=httpx.Response(200, json=ashby_board))
    respx.head(url__regex=r"https://jobs\.ashbyhq\.com/.*").mock(return_value=httpx.Response(200))
    history = HistoryConfig(entries=[HistoryEntry(company="Wealth.com", status="blacklist")])

    result = run_pipeline(
        _ashby_only(filters_cfg), client, root=tmp_path, use_state=True, history=history
    )
    assert result.suppressed  # the private pipeline did suppress everything

    from engine.public_store import PublicStore

    store = PublicStore.load(tmp_path / "data" / "public-roles.jsonl")
    titles = {r.title for r in store.open_roles()}
    # Both title-matched roles are listed despite the blacklist.
    assert titles == {"GTM Engineer", "Revenue Systems Engineer"}
    # The off-family roles never entered.
    assert "Technical Support Associate" not in titles
    # And nothing private is in the file, not even as residue.
    body = (tmp_path / "data" / "public-roles.jsonl").read_text(encoding="utf-8")
    for marker in ("blacklist", "suppress", "band-top-only", "history", "flag"):
        assert marker not in body
