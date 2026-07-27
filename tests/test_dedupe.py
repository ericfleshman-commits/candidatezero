"""The dedupe matcher.

The rule under test everywhere here is Exhibit A: on 2026-07-16 the digest
surfaced "Manifest: GTM Engineer" as a clean PASS the same day its owner had
manually disqualified that exact role. The engine must never resurface a role
its owner already ruled on.
"""

from __future__ import annotations

import pytest
import yaml
from pydantic import ValidationError

from conftest import REPO_ROOT, make_role
from engine.dedupe import (
    Deduper,
    HistoryEntry,
    load_history,
    normalize_company,
    parse_history,
    title_matches,
)

# Normalization ------------------------------------------------------------


def test_normalize_company_strips_branding_suffixes():
    assert normalize_company("Wealth.com") == "wealth"
    assert normalize_company("Tonic AI") == "tonic"
    assert normalize_company("Listen Labs") == "listen"
    assert normalize_company("Globex, Inc.") == "globex"
    assert normalize_company("Acme HQ") == "acme"


def test_normalize_company_strips_stacked_suffixes():
    assert normalize_company("Globex Labs Inc") == "globex"


def test_normalize_company_never_strips_to_nothing():
    """A company literally named after a suffix token keeps its name."""
    assert normalize_company("Labs") == "labs"
    assert normalize_company("AI") == "ai"


def test_normalize_company_keeps_real_words():
    assert normalize_company("Defense Unicorns") == "defense unicorns"
    assert normalize_company("1Password") == "1password"


def test_normalize_company_is_caseless_and_punctuation_blind():
    assert normalize_company("WEALTH-COM") == normalize_company("wealth.com")


def test_normalize_company_strips_domain_costume_prefixes():
    """Exhibit B: Profound's board lives at the slug "Tryprofound". The glued
    prefix comes off, so the board name and the history name meet."""
    assert normalize_company("Tryprofound") == "profound"
    assert normalize_company("Getcabal") == "cabal"
    assert normalize_company("Joinpaddle") == "paddle"
    assert normalize_company("The Trade Desk") == "trade desk"


def test_normalize_company_strips_glued_and_new_suffixes():
    assert normalize_company("ProfoundHQ") == "profound"
    assert normalize_company("Portainer.io") == "portainer"
    assert normalize_company("Clay App") == "clay"
    assert normalize_company("Notion Co") == "notion"


def test_normalize_company_leaves_short_stems_alone():
    """Theory is not "ory" and Getty is not "ty". A glued strip only happens
    when at least four characters of name remain."""
    assert normalize_company("Getty") == "getty"
    assert normalize_company("Theory") == "theory"
    assert normalize_company("Heyday") == "heyday"
    assert normalize_company("Useful") == "useful"
    assert normalize_company("Cisco") == "cisco"


# Title family matching ----------------------------------------------------


def test_title_matches_across_seniority():
    assert title_matches("GTM Engineer", "Senior GTM Engineer")
    assert title_matches("GTM Engineer", "GTM Engineer, Seller Efficiency")


def test_title_matches_collapses_go_to_market():
    assert title_matches("Go-To-Market Engineer", "GTM Engineer")


def test_title_matches_rejects_different_families():
    assert not title_matches("GTM Engineer", "Account Executive")
    assert not title_matches("RevOps Engineer", "Growth Engineer")


def test_title_matches_rejects_empty():
    assert not title_matches("", "GTM Engineer")


# Suppression scope --------------------------------------------------------


def _deduper(*entries: HistoryEntry) -> Deduper:
    return Deduper(list(entries))


def test_dq_suppresses_every_role_at_the_company():
    """Exhibit A. Manifest was dq'd as a software engineer role in costume;
    nothing from Manifest may surface again, whatever the title."""
    d = _deduper(
        HistoryEntry(
            company="Manifest",
            status="dq",
            role="GTM Engineer",
            reason="software engineer seat in costume",
        )
    )
    assert d.match(make_role(company_name="Manifest", title="GTM Engineer")) is not None
    assert d.match(make_role(company_name="Manifest", title="Growth Engineer")) is not None
    assert d.match(make_role(company_name="Manifest", title="Head of Sales")) is not None


def test_blacklist_suppresses_every_role_at_the_company():
    d = _deduper(HistoryEntry(company="Revin", status="blacklist"))
    assert d.match(make_role(company_name="Revin", title="GTM Engineer")) is not None
    assert d.match(make_role(company_name="Revin", title="RevOps Architect")) is not None


def test_company_wide_status_outranks_role_level_regardless_of_order():
    d = _deduper(
        HistoryEntry(company="Revin", status="applied", role="GTM Engineer"),
        HistoryEntry(company="Revin", status="blacklist"),
    )
    matched = d.match(make_role(company_name="Revin", title="RevOps Engineer"))
    assert matched is not None and matched.status == "blacklist"


def test_role_level_status_suppresses_only_the_matching_role():
    d = _deduper(
        HistoryEntry(company="Acme", status="applied", role="GTM Engineer", date="2026-07-01")
    )
    assert d.match(make_role(company_name="Acme", title="Senior GTM Engineer")) is not None
    # A different role at the same company stays live.
    assert d.match(make_role(company_name="Acme", title="Data Engineer")) is None


def test_a_live_role_at_a_known_company_gets_a_history_note():
    d = _deduper(
        HistoryEntry(company="Acme", status="rejected", role="GTM Engineer", date="2026-06-20")
    )
    role = make_role(company_name="Acme", title="RevOps Engineer")
    assert d.match(role) is None
    assert d.company_note(role) == "history at this company: rejected 2026-06-20"


def test_no_history_no_note():
    d = _deduper(HistoryEntry(company="Acme", status="applied"))
    assert d.company_note(make_role(company_name="Someone Else")) is None


def test_entry_without_a_role_rules_on_the_whole_company():
    """A ruling you cannot scope to a title is a ruling on the company."""
    d = _deduper(HistoryEntry(company="Acme", status="applied"))
    assert d.match(make_role(company_name="Acme", title="Anything At All")) is not None


def test_company_match_survives_branding_differences():
    d = _deduper(HistoryEntry(company="Wealth.com", status="rejected", role="GTM Engineer"))
    assert d.match(make_role(company_name="Wealth", title="GTM Engineer")) is not None


def test_tryprofound_board_matches_profound_history():
    """The real miss from the 2026-07-25 run: Profound was applied-to, but the
    board's display name is Tryprofound and the digest flagged it as new."""
    d = _deduper(HistoryEntry(company="Profound", status="applied", role="GTM Engineer"))
    assert d.match(make_role(company_name="Tryprofound", title="GTM Engineer")) is not None
    # And the same connection made from the other side.
    d2 = _deduper(HistoryEntry(company="Tryprofound", status="applied", role="GTM Engineer"))
    assert d2.match(make_role(company_name="Profound", title="GTM Engineer")) is not None


# Loading ------------------------------------------------------------------


def test_parse_history_reads_the_legacy_flat_lists():
    entries = parse_history(
        {
            "applied": ["Acme"],
            "interviewing": ["Initech"],
            "rejected": ["Globex"],
            "blacklist_companies": ["Globex"],
        }
    )
    statuses = {(e.company, e.status) for e in entries}
    assert statuses == {
        ("Acme", "applied"),
        ("Initech", "interviewing"),
        ("Globex", "rejected"),
        ("Globex", "blacklist"),
    }
    assert all(e.role is None for e in entries)


def test_parse_history_reads_both_schemas_together():
    entries = parse_history(
        {
            "history": [{"company": "Acme", "status": "dq", "date": "2026-07-16"}],
            "rejected": ["Globex"],
        }
    )
    assert len(entries) == 2


def test_yaml_dates_become_iso_strings():
    raw = yaml.safe_load("history:\n  - {company: Acme, status: applied, date: 2026-07-16}\n")
    entries = parse_history(raw)
    assert entries[0].date == "2026-07-16"


def test_the_shipped_example_history_loads(tmp_path):
    """The committed example is the documentation; it must always parse."""
    raw = yaml.safe_load(
        (REPO_ROOT / "config" / "example" / "history.yaml").read_text(encoding="utf-8")
    )
    entries = parse_history(raw)
    assert entries, "example history.yaml parsed to nothing"
    statuses = {e.status for e in entries}
    assert {"applied", "interviewing", "rejected", "dq", "blacklist"} <= statuses


def test_load_history_missing_file_is_empty_not_an_error(tmp_path):
    cfg = load_history(root=tmp_path)
    assert cfg.entries == []
    assert cfg.source == "none"


def test_load_history_private_beats_example(tmp_path):
    private = tmp_path / "config" / "private"
    example = tmp_path / "config" / "example"
    private.mkdir(parents=True)
    example.mkdir(parents=True)
    (example / "history.yaml").write_text("rejected: [Example Co]\n", encoding="utf-8")
    (private / "history.yaml").write_text(
        "history:\n  - {company: Real Co, status: dq, reason: swe in costume}\n",
        encoding="utf-8",
    )

    cfg = load_history(root=tmp_path)

    assert cfg.source == "private"
    assert [e.company for e in cfg.entries] == ["Real Co"]


def test_a_malformed_entry_fails_loudly_not_silently():
    """An unquoted comma in a flow-style reason parses into bogus extra keys.
    Swallowing them would silently truncate a ruling; refuse instead."""
    raw = yaml.safe_load("history:\n  - { company: Acme, status: dq, reason: one, two }\n")
    with pytest.raises(ValidationError):
        parse_history(raw)
