"""Filter semantics, tested against the config the repo actually ships."""

from __future__ import annotations

import pytest

from conftest import load_fixture, make_role
from engine.filters import FilterEngine, normalize_title
from engine.models import CompRange


@pytest.fixture
def engine(filters_cfg) -> FilterEngine:
    return FilterEngine(filters_cfg)


# Title ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("Go-To-Market Engineer", "gtm engineer"),
        ("Go To Market Engineer", "gtm engineer"),
        ("GTM Engineer", "gtm engineer"),
        ("Senior Go-to-Market Engineer, Growth", "senior gtm engineer growth"),
        ("RevOps Engineer (Remote)", "revops engineer remote"),
    ],
)
def test_every_spelling_of_go_to_market_collapses_to_gtm(raw, expected):
    assert normalize_title(raw) == expected


@pytest.mark.parametrize(
    "title",
    [
        "GTM Engineer",
        "Go-To-Market Engineer",
        "Senior GTM Systems Architect",
        "Revenue Operations Engineer",
        "Enterprise Application Data Architect, GTM Systems",
    ],
)
def test_target_titles_are_included(engine, title):
    assert engine.title_family(make_role(title=title)) == "include"


def test_growth_engineer_is_flagged_never_dropped(engine):
    role = make_role(
        title="Growth Engineer",
        comp=CompRange(min=200000, max=250000, source="structured"),
    )
    verdict = engine.evaluate(role)

    assert engine.title_family(role) == "flag"
    assert verdict.decision == "flag"
    assert "title-family-flag" in role.flags


def test_unrelated_titles_are_dropped_on_title(engine):
    verdict = engine.evaluate(make_role(title="Staff Backend Engineer"))
    assert verdict.decision == "drop"
    assert verdict.reasons == ["title"]


# Location ------------------------------------------------------------------


@pytest.mark.parametrize(
    "location",
    [
        "New York City",
        "Brooklyn",
        "Manhattan, NY",
        "Austin | Chicago | New York City | Salt Lake City",
        "Remote (US)",
        "Remote, United States",
        "Remote",
    ],
)
def test_nyc_and_us_remote_pass(engine, location):
    assert engine.location_ok(make_role(location_raw=location)) is True


@pytest.mark.parametrize(
    "location",
    ["Remote (EMEA)", "Remote - Europe", "Remote, Canada", "Remote (APAC)", "Remote - India"],
)
def test_remote_qualified_by_a_non_us_region_fails(engine, location):
    assert engine.location_ok(make_role(location_raw=location)) is False


def test_region_tokens_match_on_word_boundaries(engine):
    """A bare uk substring would otherwise blacklist Ukraine and Fukuoka."""
    assert engine.location_ok(make_role(location_raw="Remote, Ukraine")) is True


def test_multi_location_passes_if_any_segment_matches(engine):
    assert engine.location_ok(make_role(location_raw="Dublin | Tel Aviv | New York City")) is True


def test_a_plain_non_target_city_fails(engine):
    assert engine.location_ok(make_role(location_raw="Dublin")) is False


def test_ashby_is_remote_flag_counts_as_remote(engine):
    assert engine.location_ok(make_role(location_raw="Phoenix, AZ", is_remote=True)) is True


# The remote geography gap ---------------------------------------------------


@pytest.mark.parametrize(
    "location",
    ["Remote (US)", "Remote, United States", "Remote - USA", "Remote, NY", "Remote | Austin"],
)
def test_remote_with_a_us_signal_is_verified(engine, location):
    assert engine.location_status(make_role(location_raw=location)) == "ok"


@pytest.mark.parametrize("location", ["Remote", "Remote, Lisbon", "Remote - Worldwide"])
def test_remote_without_a_us_signal_is_flagged_never_dropped(engine, location):
    """The closed gap: a role marked remote out of a non-US city used to sail
    through as if it were US remote. Now it survives, carrying a flag."""
    role = make_role(
        location_raw=location,
        comp=CompRange(min=180000, max=220000, source="structured"),
    )
    assert engine.location_status(role) == "remote-unverified"

    verdict = engine.evaluate(role)
    assert verdict.decision == "flag"
    assert "remote-geo-unverified" in role.flags
    assert any("no US signal" in reason for reason in verdict.reasons)


def test_state_codes_only_count_after_a_comma(engine):
    """Matched bare, Indiana's "in" would make "Remote in Ireland" look American."""
    ireland = make_role(location_raw="Remote in Ireland")
    assert engine.location_status(ireland) == "remote-unverified"
    assert engine.location_status(make_role(location_raw="Remote, Indianapolis, IN")) == "ok"


def test_us_tokens_match_on_word_boundaries(engine):
    """A bare "us" substring would otherwise turn "status" into a US signal."""
    role = make_role(location_raw="Remote - status pending")
    assert engine.location_status(role) == "remote-unverified"


def test_ashby_remote_without_location_text_is_unverified(engine):
    """Ashby can mark a role remote with an empty location string. No string,
    no US signal, so it carries the flag."""
    role = make_role(
        location_raw="",
        is_remote=True,
        comp=CompRange(min=180000, max=220000, source="structured"),
    )
    assert engine.location_status(role) == "remote-unverified"
    assert engine.evaluate(role).decision == "flag"


# Shape: what the JD actually asks for ---------------------------------------

CLEAN_BAND = CompRange(min=180000, max=220000, source="structured")


@pytest.mark.parametrize(
    "pack, description",
    [
        (
            "shape-swe",
            "You will own our CI/CD pipeline, review production code, and ship React features.",
        ),
        (
            "shape-sales",
            "Carry a quota, own pipeline generation, and bring closing experience.",
        ),
        (
            "shape-junior",
            "Perfect for early career candidates with 1-3 years of experience.",
        ),
        (
            "shape-agency",
            "Our client, a confidential company, engaged our recruiting firm for this search.",
        ),
    ],
)
def test_each_shape_pack_flags_on_jd_language(engine, pack, description):
    """One test per pack: a hit moves PASS to FLAGGED, never to drop."""
    role = make_role(description_text=description, comp=CLEAN_BAND)
    verdict = engine.evaluate(role)

    assert verdict.decision == "flag"
    assert pack in role.flags
    assert any(reason.startswith(f"{pack}:") for reason in verdict.reasons)


def test_the_digest_line_quotes_the_exact_matched_text(engine):
    """Not the config phrase, the JD's own spelling of it. "CI / CD" with
    spaces is what this posting wrote, so that is what gets quoted."""
    role = make_role(
        description_text="We ship through a CI / CD pipeline every day.",
        comp=CLEAN_BAND,
    )
    verdict = engine.evaluate(role)

    assert 'shape-swe: JD says "CI / CD"' in verdict.reasons


def test_swe_vocabulary_goes_beyond_named_stacks(engine):
    """OpenAI's Product Engineer, GTM Growth Engineering JD names no stack at
    all; it says "full-stack" and "frontend" instead. The generic vocabulary
    of SWE ads has to hit too, hyphenated or not."""
    role = make_role(
        description_text="Own full-stack product slices, from frontend to backend APIs.",
        comp=CLEAN_BAND,
    )
    verdict = engine.evaluate(role)

    assert "shape-swe" in role.flags
    assert 'shape-swe: JD says "full-stack"' in verdict.reasons


def test_a_role_can_carry_multiple_shape_flags(engine):
    role = make_role(
        description_text=(
            "Carry a quota while shipping React dashboards. "
            "Great for early career folks. Our client is confidential."
        ),
        comp=CLEAN_BAND,
    )
    verdict = engine.evaluate(role)

    assert verdict.decision == "flag"
    for pack in ("shape-swe", "shape-sales", "shape-junior", "shape-agency"):
        assert pack in role.flags


def test_shapes_read_the_title_too(engine):
    role = make_role(title="GTM Engineer (via Staffing Partner)", comp=CLEAN_BAND)
    engine.evaluate(role)
    assert "shape-agency" in role.flags


def test_shape_phrases_do_not_fire_inside_longer_words(engine):
    """"reacting" is not react, "associates" is not associate, "quotable" is
    not quota. Word boundaries keep the packs honest."""
    role = make_role(
        description_text="Reacting quickly, coordinating associates across quotable teams.",
        comp=CLEAN_BAND,
    )
    verdict = engine.evaluate(role)

    assert verdict.decision == "pass"
    assert role.flags == []


def test_a_clean_gtm_jd_stays_a_clean_pass(engine):
    role = make_role(
        description_text=(
            "Own our GTM systems: CRM hygiene, routing, enrichment, and the "
            "automation between marketing and sales."
        ),
        comp=CLEAN_BAND,
    )
    verdict = engine.evaluate(role)

    assert verdict.decision == "pass"
    assert role.flags == []


def test_a_missing_description_is_not_a_shape_hit(engine):
    """Greenhouse roles that never earned a detail fetch have no description.
    No text, no evidence, no flag."""
    role = make_role(description_text=None, comp=CLEAN_BAND)
    assert engine.evaluate(role).decision == "pass"


def test_manifest_gtm_engineer_is_flagged_shape_swe(engine):
    """The posting that motivated the packs, recorded live on 2026-07-25.

    Manifest's GTM Engineer is customer-facing web development with A/B
    testing: a software-engineer seat wearing a GTM title. Title, location,
    and band are all clean, so before shape rules this was a clean PASS. The
    engine must read the JD and doubt it on its own.
    """
    job = load_fixture("manifest_gtm_engineer.json")
    salary = job["compensation"]["salary"]
    role = make_role(
        id="ashby:manifest-os:d2619b83",
        org_slug="manifest-os",
        company_name="Manifest",
        title=job["title"],
        location_raw=job["location"],
        is_remote=job["isRemote"],
        description_text=job["descriptionPlain"],
        url=job["jobUrl"],
        comp=CompRange(
            min=salary["minValue"],
            max=salary["maxValue"],
            currency=salary["currencyCode"],
            source="structured",
        ),
    )
    verdict = engine.evaluate(role)

    assert verdict.decision == "flag"
    assert "shape-swe" in role.flags
    quoted = [r for r in verdict.reasons if r.startswith("shape-swe:")]
    assert quoted and 'JD says "' in quoted[0]


# Comp ----------------------------------------------------------------------


def test_band_fully_above_the_floor_is_a_clean_pass(engine):
    role = make_role(comp=CompRange(min=160000, max=230000, source="structured"))
    verdict = engine.evaluate(role)

    assert verdict.decision == "pass"
    assert role.flags == []


def test_band_top_only_is_kept_and_flagged(engine, filters_cfg):
    """A band straddling the floor. The job might pay enough, so surface it.

    Derived from the config rather than hardcoded, so changing the shipped
    example floor can never silently invalidate this test.
    """
    floor = filters_cfg.comp.floor_usd
    role = make_role(
        comp=CompRange(min=floor - 30000, max=floor + 20000, source="structured")
    )
    verdict = engine.evaluate(role)

    assert verdict.decision == "flag"
    assert "band-top-only" in role.flags


def test_band_entirely_below_the_floor_is_dropped(engine):
    role = make_role(comp=CompRange(min=90000, max=120000, source="structured"))
    verdict = engine.evaluate(role)

    assert verdict.decision == "drop"
    assert verdict.reasons == ["comp"]


def test_unknown_comp_is_a_flag_not_a_failure(engine):
    """NYC pay transparency means a missing band is a question, not a no."""
    role = make_role(comp=CompRange(source="none"))
    verdict = engine.evaluate(role)

    assert verdict.decision == "flag"
    assert "comp-unknown" in role.flags


def test_a_big_enough_band_buys_back_a_location_miss(engine):
    """Eric's real config carries this rule. Money moves people."""
    role = make_role(
        location_raw="San Francisco",
        comp=CompRange(min=230000, max=385000, source="structured"),
    )
    verdict = engine.evaluate(role)

    assert verdict.decision == "flag"
    assert "location-exception-comp" in role.flags


def test_the_exception_does_not_rescue_an_ordinary_band(engine, filters_cfg):
    """Clears the floor, misses the location exception. Still dropped."""
    floor = filters_cfg.comp.floor_usd
    exception = filters_cfg.location.kill_exception_comp_usd
    role = make_role(
        location_raw="San Francisco",
        comp=CompRange(min=floor + 5000, max=exception - 5000, source="structured"),
    )
    assert engine.evaluate(role).decision == "drop"


def test_location_miss_with_unknown_comp_is_still_dropped(engine, filters_cfg):
    """No band means no evidence the exception applies. Do not guess."""
    role = make_role(location_raw="Dublin", comp=CompRange(source="none"))
    assert engine.evaluate(role).decision == "drop"


# The expensive-work gate ----------------------------------------------------


def test_wants_detail_is_false_for_a_title_miss(engine):
    assert engine.wants_detail(make_role(title="Staff Backend Engineer")) is False


def test_wants_detail_is_true_for_a_title_and_location_match(engine):
    assert engine.wants_detail(make_role(title="GTM Engineer")) is True
