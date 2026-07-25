"""The registry store.

The rules under test: append-or-update by (vendor, slug), never delete a row,
and every status transition leaves a dated note behind. The registry is the
asset, and an asset that silently forgets things is not one.
"""

from __future__ import annotations

from datetime import date

from engine.registry import OrgRecord, Registry


def make_record(**kwargs) -> OrgRecord:
    base = dict(
        company_name="Gong",
        domain="gong.io",
        vendor="greenhouse",
        slug="gongio",
        board_url="https://boards.greenhouse.io/gongio",
        status="unverified",
        discovered_via="seed:companies.yaml",
    )
    base.update(kwargs)
    return OrgRecord(**base)


def test_roundtrip_through_jsonl(tmp_path):
    path = tmp_path / "registry.jsonl"
    reg = Registry(path)
    reg.upsert(make_record())
    reg.upsert(make_record(vendor="ashby", slug="wealth-com", company_name="Wealth.com"))
    reg.save()

    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2

    loaded = Registry.load(path)
    assert loaded.get("greenhouse", "gongio").company_name == "Gong"
    assert loaded.get("ashby", "wealth-com").company_name == "Wealth.com"


def test_a_corrupt_line_is_skipped_not_fatal(tmp_path):
    path = tmp_path / "registry.jsonl"
    good = make_record().model_dump_json()
    path.write_text(good + "\nnot json at all\n", encoding="utf-8")

    loaded = Registry.load(path)
    assert len(loaded.records) == 1


def test_upsert_is_keyed_by_vendor_and_slug(tmp_path):
    reg = Registry(tmp_path / "registry.jsonl")
    reg.upsert(make_record())
    reg.upsert(make_record())  # same key, not a second row
    reg.upsert(make_record(vendor="lever"))  # same slug, different vendor: a new row

    assert len(reg.records) == 2


def test_upsert_fills_blanks_but_never_overwrites_history(tmp_path):
    reg = Registry(tmp_path / "registry.jsonl")
    first = reg.upsert(make_record(domain="", notes="hand-checked once"))
    reg.mark_live("greenhouse", "gongio", 102, when=date(2026, 7, 20))

    again = reg.upsert(make_record(domain="gong.io", discovered_via="seed:domains"))

    assert again is first
    assert again.domain == "gong.io"  # the blank was filled
    assert again.status == "live"  # rediscovery did not reset verification
    assert again.last_posting_count == 102
    assert "hand-checked once" in again.notes
    assert again.discovered_via == "seed:companies.yaml"  # first discovery wins


def test_live_to_dead_leaves_a_dated_note_and_keeps_the_row(tmp_path):
    reg = Registry(tmp_path / "registry.jsonl")
    reg.upsert(make_record())
    reg.mark_live("greenhouse", "gongio", 102, when=date(2026, 7, 20))

    record = reg.mark_dead(
        "greenhouse", "gongio", when=date(2026, 7, 25), reason="404 on boards-api"
    )

    assert record.status == "dead"
    assert record.last_posting_count == 0
    assert record.last_verified == date(2026, 7, 25)
    assert "went dead on 2026-07-25: 404 on boards-api" in record.notes
    assert reg.get("greenhouse", "gongio") is not None  # never deleted


def test_dead_to_live_is_a_resurrection_with_a_note(tmp_path):
    """The chainalysis-careers rule: a dead verdict is allowed to be wrong later."""
    reg = Registry(tmp_path / "registry.jsonl")
    reg.upsert(make_record())
    reg.mark_dead("greenhouse", "gongio", when=date(2026, 7, 20), reason="404")

    record = reg.mark_live("greenhouse", "gongio", 48, when=date(2026, 7, 25))

    assert record.status == "live"
    assert record.last_posting_count == 48
    assert "back from the dead on 2026-07-25 with 48 postings" in record.notes
    assert "went dead on 2026-07-20" in record.notes  # history accumulates


def test_marking_dead_twice_does_not_stack_notes(tmp_path):
    reg = Registry(tmp_path / "registry.jsonl")
    reg.upsert(make_record())
    reg.mark_dead("greenhouse", "gongio", when=date(2026, 7, 20), reason="404")
    reg.mark_dead("greenhouse", "gongio", when=date(2026, 7, 21), reason="404")

    assert reg.get("greenhouse", "gongio").notes.count("went dead") == 1


def test_live_filter_and_stats(tmp_path):
    reg = Registry(tmp_path / "registry.jsonl")
    reg.upsert(make_record())
    reg.mark_live("greenhouse", "gongio", 102, when=date(2026, 7, 25))
    reg.upsert(make_record(vendor="ashby", slug="wealth-com", company_name="Wealth.com"))
    reg.mark_live("ashby", "wealth-com", 4, when=date(2026, 7, 25))
    reg.upsert(make_record(vendor="lever", slug="ghosts", company_name="Ghosts"))
    reg.mark_dead("lever", "ghosts", when=date(2026, 7, 25), reason="404")
    reg.upsert(make_record(vendor="workable", slug="maybe", company_name="Maybe"))
    reg.upsert(make_record(vendor="unknown", slug="opaque.example", domain="opaque.example"))

    assert [r.slug for r in reg.live()] == ["wealth-com", "gongio"]
    assert [r.slug for r in reg.live(vendor="ashby")] == ["wealth-com"]

    stats = reg.stats()
    assert stats.total == 5
    assert stats.live == 2
    assert stats.live_by_vendor == {"ashby": 1, "greenhouse": 1}
    assert stats.dead == 1
    assert stats.unverified == 1
    assert stats.no_ats_found == 1
    assert stats.postings_last_seen == 106


def test_domains_seen_is_the_resume_marker(tmp_path):
    reg = Registry(tmp_path / "registry.jsonl")
    reg.upsert(make_record())
    reg.upsert(make_record(vendor="unknown", slug="opaque.example", domain="opaque.example"))

    assert reg.domains_seen() == {"gong.io", "opaque.example"}
