"""Digest rendering.

The contract: kept roles are printed in full, dropped roles are counts only, and
a dead org is a footer line rather than a missing section.
"""

from __future__ import annotations

from datetime import UTC, datetime

from conftest import make_role
from engine.digest import render
from engine.models import CompRange, OrgWarning, RunResult, SuppressedRole, Verdict


def _result() -> RunResult:
    kept = make_role(
        title="GTM Engineer",
        company_name="Manifest",
        comp=CompRange(min=160000, max=230000, currency="USD", source="structured"),
        published_at=datetime(2026, 6, 18, tzinfo=UTC),
    )
    kept.verdict = Verdict(decision="pass", reasons=["band clears 160,000"])

    flagged = make_role(
        id="ashby:wealth-com:2",
        title="GTM Engineer",
        company_name="Wealth.com",
        comp=CompRange(min=130000, max=180000, currency="USD", source="structured"),
    )
    flagged.flag("band-top-only")
    flagged.verdict = Verdict(decision="flag", reasons=["only the top of the band clears 160,000"])

    return RunResult(
        orgs_scanned=8,
        roles_seen=942,
        kept=[kept],
        flagged=[flagged],
        drop_counts={"title": 935, "location": 2, "zombie": 1},
        warnings=[OrgWarning(source="greenhouse", slug="chainalysis-careers", reason="404")],
    )


def test_digest_prints_kept_roles_in_full():
    body = render(_result(), run_date=datetime(2026, 7, 16).date())

    assert "# CandidateZero digest 2026-07-16" in body
    assert "Manifest: GTM Engineer" in body
    assert "$160,000 to $230,000" in body
    assert "structured" in body
    assert "https://example.com/job/1" in body
    assert "band clears 160,000" in body


def test_header_says_new_tonight_and_clearing_all_filters():
    """The header once printed "Surviving: N" where N meant new-to-the-seen-store,
    while the footer's "survived" meant everything clearing the filters. Two
    meanings sharing one word produced a 33-vs-1 scare. The header now names
    both numbers."""
    result = _result()
    result.still_open = 3
    header = render(result, run_date=datetime(2026, 7, 16).date()).split("## New verified roles")[0]

    assert "New tonight: 2" in header  # 1 kept + 1 flagged, new to the seen store
    assert "Clearing all filters: 5" in header  # the footer's survivor count, same meaning
    assert "Surviving:" not in header


def test_digest_separates_flagged_from_verified():
    body = render(_result(), run_date=datetime(2026, 7, 16).date())
    new_section = body.split("## Flagged")[0]

    assert "Manifest" in new_section
    # A band-top-only role is worth seeing, but not in the verified section.
    assert "Wealth.com" not in new_section
    assert "band-top-only" in body.split("## Flagged")[1]


def test_footer_prints_the_funnel_in_order():
    result = _result()
    result.still_open = 3
    footer = render(result, run_date=datetime(2026, 7, 16).date()).split("## Footer")[1]

    assert "postings read: 942" in footer
    assert "eliminated at title: 935" in footer
    assert "eliminated at location: 2" in footer
    assert "eliminated at comp: 0" in footer
    assert "eliminated at liveness: 1" in footer
    assert "survived: 5" in footer  # 1 kept, 1 flagged, 3 still open
    # The stages appear in funnel order, cheap to expensive.
    positions = [
        footer.index(f"eliminated at {s}") for s in ("title", "location", "comp", "liveness")
    ]
    assert positions == sorted(positions)


def test_dropped_roles_are_counts_only_never_noise():
    body = render(_result(), run_date=datetime(2026, 7, 16).date())
    footer = body.split("## Footer")[1]

    assert "title: 935" in footer
    assert "location: 2" in footer
    assert "zombie: 1" in footer


def test_a_dead_org_is_a_footer_warning():
    body = render(_result(), run_date=datetime(2026, 7, 16).date())
    assert "chainalysis-careers" in body
    assert "404" in body


def test_an_empty_run_still_renders():
    """A quiet night is a valid outcome, not a crash."""
    body = render(RunResult(orgs_scanned=8, roles_seen=100), run_date=datetime(2026, 7, 16).date())

    assert "None tonight" in body
    assert "Nothing flagged tonight" in body


def test_digest_obeys_house_style():
    body = render(_result(), run_date=datetime(2026, 7, 16).date())
    for char in (chr(0x2014), chr(0x2192), chr(0x2190), chr(0x21D2)):
        assert char not in body


def test_closed_roles_are_listed_when_present():
    result = _result()
    result.closed = ["Acme: GTM Engineer (https://example.com/job/9)"]
    body = render(result, run_date=datetime(2026, 7, 16).date())

    assert "## Closed since last run" in body
    assert "Acme: GTM Engineer" in body


def _suppressed(n: int = 1) -> list[SuppressedRole]:
    return [
        SuppressedRole(
            company=f"Company {i}",
            title="GTM Engineer",
            status="dq",
            date="2026-07-16",
            reason="software engineer seat in costume",
        )
        for i in range(n)
    ]


def test_suppressed_roles_are_one_line_each_never_pass_or_flag():
    """The Exhibit A rendering: a suppressed Manifest lives in its own section,
    one line with status, date and reason, and nowhere above it."""
    result = _result()
    result.kept = []
    result.flagged = []
    result.suppressed = [
        SuppressedRole(
            company="Manifest",
            title="GTM Engineer",
            status="dq",
            date="2026-07-16",
            reason="software engineer seat in costume",
        )
    ]
    body = render(result, run_date=datetime(2026, 7, 25).date())

    assert "## Suppressed" in body
    section = body.split("## Suppressed")[1].split("## Footer")[0]
    assert "- Manifest: GTM Engineer (dq 2026-07-16), software engineer seat in costume" in section
    # Nothing suppressed leaks into the PASS or FLAG sections above.
    assert "Manifest" not in body.split("## Suppressed")[0]


def test_suppressed_section_is_absent_when_empty():
    body = render(_result(), run_date=datetime(2026, 7, 25).date())
    assert "## Suppressed" not in body


def test_suppressed_caps_at_fifteen_lines_and_counts_the_overflow():
    result = _result()
    result.suppressed = _suppressed(20)
    section = render(result, run_date=datetime(2026, 7, 25).date()).split("## Suppressed")[1]
    section = section.split("## Footer")[0]

    assert section.count("- Company") == 15
    assert "and 5 more suppressed" in section


def test_footer_counts_the_suppressed():
    result = _result()
    result.suppressed = _suppressed(3)
    footer = render(result, run_date=datetime(2026, 7, 25).date()).split("## Footer")[1]
    assert "suppressed by history: 3" in footer


def test_footer_suppressed_count_is_zero_without_history():
    footer = render(_result(), run_date=datetime(2026, 7, 25).date()).split("## Footer")[1]
    assert "suppressed by history: 0" in footer
