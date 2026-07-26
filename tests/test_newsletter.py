"""The weekly public report.

The hard requirement is the privacy firewall: a newsletter rendered from a
state full of private flags, suppressed roles, and personal comp rules must
not carry a trace of any of them. The firewall is structural (the module only
reads public store fields and aggregate run counts), and these tests are the
tripwire that keeps it that way.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime

from conftest import make_role
from engine import newsletter
from engine.models import CompRange
from engine.state import SeenStore

WEEK_END = date(2026, 7, 26)  # a Sunday, ISO week 30
SCANNED = {"ashby:acme", "ashby:wealth-com", "ashby:shadow"}

LAST_WEEK = datetime(2026, 7, 12, 3, 0, tzinfo=UTC)
THIS_WEEK = datetime(2026, 7, 21, 3, 0, tzinfo=UTC)


def _store() -> SeenStore:
    """A state with one of everything the firewall must handle.

    A clean new verified role, a role carrying private flags, a role the
    owner blacklisted, and a previously verified role that closes this week.
    """
    store = SeenStore()

    old_public = make_role(
        id="ashby:acme:old-1",
        title="GTM Platform Lead",
        comp=CompRange(min=170000, max=210000, currency="USD", source="structured"),
    )
    store.reconcile([old_public], SCANNED, now=LAST_WEEK)
    store.record_public([old_public])

    kept = make_role(
        id="ashby:acme:new-1",
        comp=CompRange(min=180000, max=240000, currency="USD", source="structured"),
    )
    flagged = make_role(
        id="ashby:wealth-com:flag-1",
        org_slug="wealth-com",
        company_name="FlaggedCo",
        title="Growth Engineer",
    )
    flagged.flag("band-top-only")
    flagged.flag("location-exception-comp")
    suppressed = make_role(
        id="ashby:shadow:sup-1",
        org_slug="shadow",
        company_name="BlacklistedCo",
        title="RevOps Manager",
    )

    # This week: old_public vanishes from a board that answered, the rest appear.
    store.reconcile([kept, flagged, suppressed], SCANNED, now=THIS_WEEK)
    # The pipeline only ever passes clean kept roles, but the store's guard is
    # exercised here on purpose: even handed a flagged role, it must refuse.
    store.record_public([kept, flagged])
    return store


def _runs() -> list[dict]:
    return [
        {
            "date": "2026-07-21",
            "orgs_scanned": 8,
            "postings_read": 950,
            "eliminated": {"title": 940, "location": 2, "comp": 1, "liveness": 2},
            "survivors": 5,
            "flagged": 2,
            "suppressed": 1,
            "zombies": 2,
            "dead_orgs": 0,
            "duration_seconds": 6.4,
        },
        {
            "date": "2026-07-25",
            "orgs_scanned": 8,
            "postings_read": 957,
            "eliminated": {"title": 948, "location": 2, "comp": 1, "liveness": 1},
            "survivors": 6,
            "flagged": 3,
            "suppressed": 1,
            "zombies": 1,
            "dead_orgs": 1,
            "duration_seconds": 6.5,
        },
    ]


def _body() -> str:
    report = newsletter.build_report(_store(), _runs(), WEEK_END)
    return newsletter.render(report)


def test_stat_line_aggregates_the_trailing_week():
    body = _body()
    assert "read 1,907 postings across 8 companies" in body
    assert "held 6 roles as verified live" in body
    assert "caught 3 ghost listings" in body


def test_new_section_lists_public_roles_with_band_location_and_link():
    section = _body().split("## New verified roles this week")[1].split("## Closed")[0]

    assert "**Acme: GTM Engineer**" in section
    assert "$180,000 to $240,000" in section
    assert "New York City" in section
    assert "https://example.com/job/1" in section


def test_closed_section_reports_total_and_days_alive():
    section = _body().split("## Closed this week")[1].split("## Methodology")[0]

    assert "1 role disappeared" in section
    assert "Acme: GTM Platform Lead, alive 9 days" in section


def test_a_closed_role_never_doubles_as_new():
    report = newsletter.build_report(_store(), _runs(), WEEK_END)
    assert "GTM Platform Lead" not in {r.title for r in report.new_roles}


def test_privacy_firewall_nothing_private_reaches_the_output():
    """The sprint's hard requirement. The state above contains private flags,
    a blacklisted company, suppression counts, and a personal comp floor. None
    of it may surface in the public artifact."""
    body = _body()

    # The flagged role and the blacklisted role are simply absent.
    assert "FlaggedCo" not in body
    assert "BlacklistedCo" not in body
    # No flag names, no history or blacklist language, no suppression section.
    for marker in (
        "band-top-only",
        "location-exception",
        "history-at-company",
        "blacklist",
        "suppressed",
        "Suppressed",
        "flag",
        "Flag",
    ):
        assert marker not in body, f"private marker leaked: {marker}"
    # The personal floor never appears as a number; the public framing is
    # generic. The verdict phrasing from the private digest must not leak.
    assert "clears 160,000" not in body
    assert "160,000" not in body
    assert "senior comp floors" in body


def test_methodology_footer_makes_both_promises():
    footer = _body().split("## Methodology")[1]
    assert "employer's own applicant tracking system" in footer
    assert "never applies for anyone" in footer


def test_newsletter_obeys_house_style():
    body = _body()
    for char in (chr(0x2014), chr(0x2192), chr(0x2190), chr(0x21D2)):
        assert char not in body


def test_a_quiet_week_is_published_honestly():
    """Never pad a thin week. Quiet is itself a data point."""
    body = newsletter.render(newsletter.build_report(SeenStore(), [], WEEK_END))

    assert "A quiet week: no new verified roles" in body
    assert "No roles closed this week" in body
    assert "read 0 postings" in body


def test_read_runs_takes_the_last_record_per_date(tmp_path):
    """A rerun supersedes the earlier line. Summing both would double-count."""
    path = tmp_path / "runs.jsonl"
    lines = [
        json.dumps({"date": "2026-07-25", "postings_read": 100, "survivors": 1}),
        json.dumps({"date": "2026-07-25", "postings_read": 957, "survivors": 4}),
        json.dumps({"date": "2026-07-10", "postings_read": 999}),  # outside the week
        "{not json",  # a mangled line must not kill the weekly report
        json.dumps({"date": "2026-07-24", "postings_read": 50, "survivors": 2}),
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    runs = newsletter.read_runs(path, WEEK_END)

    assert [r["date"] for r in runs] == ["2026-07-24", "2026-07-25"]
    assert sum(r["postings_read"] for r in runs) == 1007


def test_read_runs_falls_back_to_the_funnel_for_old_records():
    """Records written before the zombies field existed still count ghosts."""
    runs = [{"date": "2026-07-21", "eliminated": {"liveness": 4}}]
    report = newsletter.build_report(SeenStore(), runs, WEEK_END)
    assert report.ghosts == 4


def test_write_names_the_file_by_iso_week(tmp_path):
    report = newsletter.build_report(SeenStore(), [], WEEK_END)
    path = newsletter.write(report, tmp_path)

    assert path.name == "newsletter-2026-W30.md"
    assert "# The Verified GTM Jobs Report" in path.read_text(encoding="utf-8")
