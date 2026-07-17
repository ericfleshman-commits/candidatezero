"""The seen-store: what is new, and what quietly went away."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from conftest import make_role

from engine.state import SeenStore

MONDAY = datetime(2026, 7, 14, 3, 0, tzinfo=UTC)
TUESDAY = MONDAY + timedelta(days=1)

SCANNED = {"ashby:acme"}


def test_first_sighting_is_new():
    store = SeenStore()
    role = make_role()
    new, closed = store.reconcile([role], SCANNED, now=MONDAY)

    assert new == {role.id}
    assert closed == []
    assert store.roles[role.id].first_seen == MONDAY


def test_a_role_seen_again_is_not_new():
    store = SeenStore()
    role = make_role()
    store.reconcile([role], SCANNED, now=MONDAY)
    new, closed = store.reconcile([role], SCANNED, now=TUESDAY)

    assert new == set()
    assert closed == []
    assert store.roles[role.id].first_seen == MONDAY
    assert store.roles[role.id].last_seen == TUESDAY


def test_a_role_that_vanishes_is_closed():
    """Churn data nobody publishes. It becomes content later."""
    store = SeenStore()
    role = make_role()
    store.reconcile([role], SCANNED, now=MONDAY)
    new, closed = store.reconcile([], SCANNED, now=TUESDAY)

    assert new == set()
    assert [e.title for e in closed] == ["GTM Engineer"]
    assert store.roles[role.id].closed_at == TUESDAY


def test_a_closed_role_is_not_reported_closed_twice():
    store = SeenStore()
    store.reconcile([make_role()], SCANNED, now=MONDAY)
    store.reconcile([], SCANNED, now=TUESDAY)
    _, closed = store.reconcile([], SCANNED, now=TUESDAY + timedelta(days=1))

    assert closed == []


def test_a_404_org_does_not_mass_close_its_roles():
    """The bug this prevents is a wall of fake CLOSED lines.

    If a board times out or 404s, its roles are missing from tonight's run. That
    is an outage, not a hiring freeze. Absence only means closure for orgs that
    actually answered.
    """
    store = SeenStore()
    store.reconcile([make_role()], SCANNED, now=MONDAY)

    # Tuesday: the org failed to answer, so it is not in scanned_org_keys.
    new, closed = store.reconcile([], set(), now=TUESDAY)

    assert closed == []
    assert store.roles["ashby:acme:1"].closed_at is None


def test_a_repost_resurrects_a_closed_role():
    store = SeenStore()
    role = make_role()
    store.reconcile([role], SCANNED, now=MONDAY)
    store.reconcile([], SCANNED, now=TUESDAY)
    assert store.roles[role.id].closed_at is not None

    new, closed = store.reconcile([role], SCANNED, now=TUESDAY + timedelta(days=1))
    assert store.roles[role.id].closed_at is None


def test_store_round_trips_through_disk(tmp_path):
    store = SeenStore()
    role = make_role()
    store.reconcile([role], SCANNED, now=MONDAY)
    path = tmp_path / "seen.json"
    store.save(path)

    reloaded = SeenStore.load(path)
    assert role.id in reloaded.roles
    assert reloaded.roles[role.id].first_seen == MONDAY


def test_a_corrupt_store_does_not_crash_a_3am_run(tmp_path):
    path = tmp_path / "seen.json"
    path.write_text("{not json at all")

    store = SeenStore.load(path)
    assert store.roles == {}


def test_ids_from_two_atss_cannot_collide():
    """Greenhouse and Ashby both hand out ids. The namespace is what keeps them apart."""
    from engine.models import Role

    assert Role.make_id("ashby", "acme", 123) != Role.make_id("greenhouse", "acme", 123)
    assert Role.make_id("ashby", "acme", 123) == "ashby:acme:123"
