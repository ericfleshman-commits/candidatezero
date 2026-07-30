"""The public verified board.

This is the firewall's web-facing half, and it mirrors the newsletter test with
one crucial inversion. The newsletter publishes the roles that survived Eric's
private filters, so a private ruling makes a role ABSENT. The board publishes on
public criteria only, so a private ruling must make NO difference at all: a role
below the private comp floor, outside the private geography, or at a company
with private history is listed exactly like any other. Presence is neutral;
absence would be the leak. What must never appear is the private machinery
itself: flag names, floor values, suppression language, or the name of a
company that exists only in the private history file.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from conftest import make_role
from engine import board
from engine.dedupe import HistoryConfig, HistoryEntry
from engine.filters import FilterEngine
from engine.models import CompRange
from engine.public_store import PublicStore

NOW = datetime(2026, 7, 26, 3, 0, tzinfo=UTC)
LAST_WEEK = datetime(2026, 7, 12, 3, 0, tzinfo=UTC)
THIS_WEEK = datetime(2026, 7, 21, 3, 0, tzinfo=UTC)

SCANNED = {
    "ashby:acme",
    "ashby:beta",
    "ashby:flagco",
    "ashby:quietco",
    "greenhouse:gamma",
    "lever:gamma",
}


def _candidates() -> list:
    """One of everything the firewall must handle, plus ordinary roles.

    The private-world facts simulated here: flagged carries private flags,
    quietco is a company Eric already applied to (suppressed in the digest),
    and the software engineer roles are off-family noise.
    """
    clean_new = make_role(
        id="ashby:acme:new-1",
        comp=CompRange(min=181000, max=220000, currency="USD", source="structured"),
        published_at=datetime(2026, 7, 18, tzinfo=UTC),
    )
    old_open = make_role(
        id="ashby:acme:old-1",
        title="Revenue Systems Engineer",
        published_at=datetime(2026, 6, 2, tzinfo=UTC),
    )
    closes = make_role(
        id="ashby:beta:gone-1",
        org_slug="beta",
        company_name="Beta",
        title="GTM Engineer",
        url="https://example.com/job/beta-1",
    )
    flagged = make_role(
        id="ashby:flagco:f-1",
        org_slug="flagco",
        company_name="FlaggedCo",
        title="Growth Engineer",
        location_raw="Lisbon",
        is_remote=True,
        comp=CompRange(min=100000, max=150000, currency="USD", source="structured"),
    )
    # The private pipeline never feeds flagged roles to the store, but the
    # fixture flags this one anyway: even fed out of order, nothing may leak.
    flagged.flag("band-top-only")
    flagged.flag("shape-swe")
    flagged.flag("remote-geo-unverified")
    suppressed_live = make_role(
        id="ashby:quietco:s-1",
        org_slug="quietco",
        company_name="QuietCo",
        title="RevOps Engineer",
    )
    mirror_gh = make_role(
        id="greenhouse:gamma:g-1",
        source="greenhouse",
        org_slug="gamma",
        company_name="Gamma",
        title="RevOps Engineer",
        published_at=datetime(2026, 6, 20, tzinfo=UTC),
    )
    mirror_lever = make_role(
        id="lever:gamma:g-2",
        source="lever",
        org_slug="gamma",
        company_name="Gamma",
        title="RevOps Engineer",
        published_at=datetime(2026, 7, 1, tzinfo=UTC),
    )
    off_family_a = make_role(id="ashby:acme:x-1", title="Technical Support Associate")
    off_family_b = make_role(id="ashby:acme:x-2", title="Software Engineer")
    return [
        clean_new,
        old_open,
        closes,
        flagged,
        suppressed_live,
        mirror_gh,
        mirror_lever,
        off_family_a,
        off_family_b,
    ]


# A company that exists ONLY in Eric's private history: blacklisted, no live
# posting tonight. The board's API takes a store and run counts and nothing
# else, so this config is structurally unreachable from the render path; the
# test keeps it here as documentation and asserts the name never surfaces.
PRIVATE_HISTORY = HistoryConfig(
    entries=[
        HistoryEntry(company="VoldemortCo", status="blacklist", reason="private"),
        HistoryEntry(company="QuietCo", status="applied", role="RevOps Engineer"),
    ]
)


@pytest.fixture
def store(filters_cfg) -> PublicStore:
    """Built through the same seam the pipeline uses: title family is the one
    public criterion, applied before anything private could run."""
    fe = FilterEngine(filters_cfg)
    candidates = _candidates()
    matched = [r for r in candidates if fe.title_family(r) is not None]

    s = PublicStore()
    by_id = {r.id: r for r in matched}
    # Last week: the two Acme roles and the Beta role that will vanish.
    s.reconcile(
        [by_id["ashby:acme:old-1"], by_id["ashby:beta:gone-1"]], SCANNED, now=LAST_WEEK
    )
    # This week: Beta's board answers without its role, everything else appears.
    s.reconcile(
        [r for r in matched if r.id != "ashby:beta:gone-1"], SCANNED, now=THIS_WEEK
    )
    return s


def _runs() -> list[dict]:
    return [
        {"date": "2026-07-21", "orgs_scanned": 8, "postings_read": 950},
        {"date": "2026-07-25", "orgs_scanned": 8, "postings_read": 957},
    ]


@pytest.fixture
def body(store) -> str:
    return board.render(board.build_report(store, _runs(), now=NOW))


def test_sections_group_new_open_and_closed(store):
    report = board.build_report(store, _runs(), now=NOW)

    new_ids = {(r.company, r.title) for r in report.new_this_week}
    assert new_ids == {
        ("Acme", "GTM Engineer"),
        ("FlaggedCo", "Growth Engineer"),
        ("QuietCo", "RevOps Engineer"),
        ("Gamma", "RevOps Engineer"),
    }
    assert [(r.company, r.title) for r in report.still_open] == [
        ("Acme", "Revenue Systems Engineer")
    ]
    assert [(r.company, r.title) for r in report.closed_recently] == [("Beta", "GTM Engineer")]


def test_rows_carry_the_public_fields(body):
    assert "Acme" in body
    assert "GTM Engineer" in body
    assert "$181,000 to $220,000" in body
    assert "New York City" in body
    assert "Ashby" in body and "Greenhouse" in body
    assert "2026-07-18" in body
    assert 'href="https://example.com/job/1"' in body


def test_unpublished_comp_reads_not_published_never_invented(body):
    assert "not published" in body


def test_no_location_rule_a_remote_lisbon_role_is_listed(body):
    """The private geography rules must leave no imprint on the board."""
    assert "Lisbon (remote)" in body


def test_stats_panel_counts_from_runs_and_store(body):
    assert "Company boards read" in body and ">8<" in body
    assert "Postings read in last run" in body and ">957<" in body
    assert "Roles verified live right now" in body and ">5<" in body
    assert "Roles closed this week" in body and ">1<" in body
    assert "Last run" in body and "2026-07-21" in body


def test_postings_stat_is_the_last_run_never_a_weekly_sum(store):
    """Two runs in one week read mostly the same postings. Summing them once
    inflated the stat; the board now stands on the most recent run alone."""
    report = board.build_report(store, _runs(), now=NOW)
    assert report.postings_read == 957  # not 1,907

    report = board.build_report(store, [], now=NOW)
    assert report.postings_read == 0


def test_closed_section_reports_the_churn(body):
    assert "Beta" in body
    assert "open 9 days" in body  # 2026-07-12 to 2026-07-21


def test_cross_vendor_mirrors_render_once_with_a_note(body):
    assert body.count("RevOps Engineer") >= 2  # QuietCo's and Gamma's keeper
    assert "1 duplicate posting on other boards collapsed" in body
    assert "Lever" not in body  # the mirror itself never renders


def test_privacy_firewall_nothing_private_reaches_the_page(body, filters_cfg):
    """The sprint's hard requirement, the web-facing half of the firewall."""
    # No flag names, no suppression or history language, no shape verdicts.
    for marker in (
        "band-top-only",
        "shape-",
        "history",
        "History",
        "suppressed",
        "Suppressed",
        "blacklist",
        "flag",
    ):
        assert marker not in body, f"private marker leaked: {marker}"
    # FlaggedCo the company renders like anyone else; what it carried does not.
    assert "FlaggedCo" in body
    # The private floor and the location-exception threshold never appear as
    # numbers. Derived from config so a config change keeps the test honest.
    floor = f"{filters_cfg.comp.floor_usd:,}"
    exception = f"{filters_cfg.location.kill_exception_comp_usd:,}"
    assert floor not in body and exception not in body
    # A company that exists only in the private history file cannot surface.
    assert PRIVATE_HISTORY.entries[0].company == "VoldemortCo"
    assert "VoldemortCo" not in body


def test_the_role_count_is_exactly_the_title_matched_subset(body):
    """Off-family roles never render, private rules never subtract."""
    assert body.count('<tr class="role"') == 6  # 4 new, 1 still open, 1 closed
    assert "Technical Support Associate" not in body
    assert "Software Engineer" not in body


def test_a_privately_ruled_company_is_listed_like_any_other(body):
    """No history dedupe: absence, not presence, would leak the private rule."""
    assert "QuietCo" in body
    assert "applied" not in body  # the ruling itself stays private


def test_methodology_footer_makes_both_promises(body):
    assert "employer's own applicant tracking system" in body
    assert "never applies for anyone" in body


def test_board_obeys_house_style(body):
    for char in (chr(0x2014), chr(0x2192), chr(0x2190), chr(0x21D2)):
        assert char not in body


def test_a_quiet_board_is_published_honestly():
    report = board.build_report(PublicStore(), [], now=NOW)
    page = board.render(report)

    assert "No new roles this week" in page
    assert "No roles closed in the last 7 days" in page
    assert report.verified_live == 0 and report.last_run == "never"


def test_third_party_text_is_html_escaped(filters_cfg):
    fe = FilterEngine(filters_cfg)
    role = make_role(company_name="Acme <script>alert(1)</script> & Co", title="GTM Engineer")
    assert fe.title_family(role) is not None
    s = PublicStore()
    s.reconcile([role], SCANNED, now=THIS_WEEK)

    page = board.render(board.build_report(s, [], now=NOW))
    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;" in page


def test_write_lands_at_data_board_index(tmp_path, store):
    report = board.build_report(store, _runs(), now=NOW)
    path = board.write(report, tmp_path)

    assert path == tmp_path / "board" / "index.html"
    assert "<!doctype html>" in path.read_text(encoding="utf-8")
