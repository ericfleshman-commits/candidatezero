"""The public listing store: what the board is allowed to know.

The firewall property under test: PublicRole has no field a private rule could
travel in, so even a store fed flagged roles serializes nothing private.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from conftest import make_role
from engine.models import CompRange
from engine.public_store import PublicStore

MONDAY = datetime(2026, 7, 13, 3, 0, tzinfo=UTC)
TUESDAY = MONDAY + timedelta(days=1)

SCANNED = {"ashby:acme"}


def test_first_sighting_records_the_public_fields():
    store = PublicStore()
    role = make_role(
        comp=CompRange(min=150000, max=190000, currency="USD", source="structured"),
        is_remote=True,
        published_at=datetime(2026, 7, 1, tzinfo=UTC),
    )
    store.reconcile([role], SCANNED, now=MONDAY)

    entry = store.roles[role.id]
    assert entry.company == "Acme" and entry.vendor == "ashby"
    assert entry.comp_band == "$150,000 to $190,000"
    assert entry.location == "New York City" and entry.remote is True
    assert entry.posted_at == datetime(2026, 7, 1, tzinfo=UTC)
    assert entry.first_seen == entry.last_seen == entry.last_verified == MONDAY
    assert entry.closed_at is None


def test_an_unpublished_band_stays_empty_not_invented():
    store = PublicStore()
    store.reconcile([make_role()], SCANNED, now=MONDAY)
    assert store.roles["ashby:acme:1"].comp_band == ""


def test_a_resighting_updates_last_seen_and_last_verified_only():
    store = PublicStore()
    role = make_role()
    store.reconcile([role], SCANNED, now=MONDAY)
    store.reconcile([role], SCANNED, now=TUESDAY)

    entry = store.roles[role.id]
    assert entry.first_seen == MONDAY
    assert entry.last_seen == entry.last_verified == TUESDAY


def test_a_vanished_role_is_closed_only_when_its_org_answered():
    store = PublicStore()
    store.reconcile([make_role()], SCANNED, now=MONDAY)

    store.reconcile([], set(), now=TUESDAY)  # org did not answer: outage, not closure
    assert store.roles["ashby:acme:1"].closed_at is None

    store.reconcile([], SCANNED, now=TUESDAY)
    assert store.roles["ashby:acme:1"].closed_at == TUESDAY


def test_a_repost_resurrects_a_closed_role():
    store = PublicStore()
    role = make_role()
    store.reconcile([role], SCANNED, now=MONDAY)
    store.reconcile([], SCANNED, now=TUESDAY)
    store.reconcile([role], SCANNED, now=TUESDAY + timedelta(days=1))

    entry = store.roles[role.id]
    assert entry.closed_at is None and entry.first_seen == MONDAY


def test_a_closed_role_is_dropped_after_the_retention_week():
    store = PublicStore()
    store.reconcile([make_role()], SCANNED, now=MONDAY)
    store.reconcile([], SCANNED, now=TUESDAY)

    store.reconcile([], SCANNED, now=TUESDAY + timedelta(days=7))
    assert "ashby:acme:1" in store.roles  # day 7: still churn content

    store.reconcile([], SCANNED, now=TUESDAY + timedelta(days=8))
    assert "ashby:acme:1" not in store.roles


def test_cross_vendor_mirrors_collapse_into_the_earliest_posted():
    store = PublicStore()
    early = make_role(
        id="greenhouse:acme:9",
        source="greenhouse",
        published_at=datetime(2026, 6, 1, tzinfo=UTC),
    )
    late = make_role(published_at=datetime(2026, 7, 1, tzinfo=UTC))  # ashby mirror
    store.reconcile([early, late], {"ashby:acme", "greenhouse:acme"}, now=MONDAY)

    assert store.roles["greenhouse:acme:9"].collapsed_into is None
    assert store.roles["ashby:acme:1"].collapsed_into == "greenhouse:acme:9"
    assert [r.id for r in store.open_roles()] == ["greenhouse:acme:9"]


def test_same_vendor_same_title_postings_are_distinct_requisitions():
    store = PublicStore()
    nyc = make_role(id="ashby:acme:1", location_raw="New York City")
    sf = make_role(id="ashby:acme:2", location_raw="San Francisco")
    store.reconcile([nyc, sf], SCANNED, now=MONDAY)

    assert len(store.open_roles()) == 2


def test_a_closing_keeper_hands_the_listing_back_to_its_mirror():
    store = PublicStore()
    early = make_role(
        id="greenhouse:acme:9",
        source="greenhouse",
        published_at=datetime(2026, 6, 1, tzinfo=UTC),
    )
    late = make_role(published_at=datetime(2026, 7, 1, tzinfo=UTC))
    both = {"ashby:acme", "greenhouse:acme"}
    store.reconcile([early, late], both, now=MONDAY)
    store.reconcile([late], both, now=TUESDAY)  # the greenhouse posting closes

    assert store.roles["ashby:acme:1"].collapsed_into is None
    assert [r.id for r in store.open_roles()] == ["ashby:acme:1"]


def test_flags_never_reach_the_file(tmp_path):
    """A flagged role fed to the store serializes with zero private residue.

    The pipeline feeds this store before flags exist, but the guarantee must
    hold even against a caller that gets the order wrong.
    """
    store = PublicStore()
    role = make_role()
    role.flag("band-top-only")
    role.flag("shape-swe")
    role.flag("history-at-company")
    store.reconcile([role], SCANNED, now=MONDAY)

    path = tmp_path / "public-roles.jsonl"
    store.save(path)
    body = path.read_text(encoding="utf-8")
    for marker in ("band-top-only", "shape-", "history", "flag", "verdict", "suppress"):
        assert marker not in body, f"private residue in the public store: {marker}"


def test_store_round_trips_through_disk(tmp_path):
    store = PublicStore()
    store.reconcile([make_role()], SCANNED, now=MONDAY)
    path = tmp_path / "public-roles.jsonl"
    store.save(path)

    reloaded = PublicStore.load(path)
    assert reloaded.roles["ashby:acme:1"].first_seen == MONDAY


def test_a_mangled_line_is_skipped_not_fatal(tmp_path):
    store = PublicStore()
    store.reconcile([make_role()], SCANNED, now=MONDAY)
    path = tmp_path / "public-roles.jsonl"
    store.save(path)
    path.write_text(path.read_text(encoding="utf-8") + "{not json\n", encoding="utf-8")

    reloaded = PublicStore.load(path)
    assert len(reloaded.roles) == 1
