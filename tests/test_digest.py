"""Digest rendering.

The contract: kept roles are printed in full, dropped roles are counts only, and
a dead org is a footer line rather than a missing section.
"""

from __future__ import annotations

from datetime import UTC, datetime

from conftest import make_role
from engine.digest import render
from engine.models import CompRange, OrgWarning, RunResult, Verdict


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

    assert "# Candidate Zero digest 2026-07-16" in body
    assert "Manifest: GTM Engineer" in body
    assert "$160,000 to $230,000" in body
    assert "structured" in body
    assert "https://example.com/job/1" in body
    assert "band clears 160,000" in body


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
