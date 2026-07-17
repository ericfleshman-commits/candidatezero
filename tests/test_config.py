"""Config loading and the private-over-example fallback."""

from __future__ import annotations

import pytest
import yaml

from engine.config import REPO_ROOT, CompaniesConfig, load_config, resolve


def _write(root, where, name, body) -> None:
    d = root / "config" / where
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(yaml.safe_dump(body))


def test_private_wins_over_example(tmp_path):
    _write(tmp_path, "example", "filters.yaml", {"comp": {"floor_usd": 100000}})
    _write(tmp_path, "private", "filters.yaml", {"comp": {"floor_usd": 160000}})

    assert resolve("filters.yaml", tmp_path).parent.name == "private"


def test_example_is_the_fallback(tmp_path):
    _write(tmp_path, "example", "filters.yaml", {"comp": {"floor_usd": 100000}})

    assert resolve("filters.yaml", tmp_path).parent.name == "example"


def test_resolution_is_per_file_not_per_directory(tmp_path):
    """A private filters.yaml must not hide the example companies.yaml.

    This is what lets a fresh clone run off the committed example config while
    the owner overrides only the files they actually wrote.
    """
    _write(tmp_path, "example", "filters.yaml", {"comp": {"floor_usd": 100000}})
    _write(tmp_path, "example", "companies.yaml", {"ashby": [{"slug": "wealth-com"}]})
    _write(tmp_path, "private", "filters.yaml", {"comp": {"floor_usd": 160000}})

    cfg = load_config(tmp_path)

    assert cfg.filters.comp.floor_usd == 160000
    assert cfg.sources["filters.yaml"] == "private"
    assert cfg.sources["companies.yaml"] == "example"
    assert cfg.companies.ashby[0].slug == "wealth-com"


def test_missing_config_is_a_clear_error(tmp_path):
    with pytest.raises(FileNotFoundError, match="companies.yaml"):
        load_config(tmp_path)


def test_the_shipped_example_config_actually_loads():
    """The committed example config doubles as documentation. It has to work."""
    cfg = load_config(REPO_ROOT)

    assert cfg.companies.active(), "example companies.yaml lists no orgs"
    assert cfg.filters.comp.floor_usd > 0
    assert "gtm engineer" in cfg.filters.title_families.include


def test_dead_orgs_are_never_probed():
    companies = CompaniesConfig(
        ashby=[{"slug": "wealth-com"}],
        dead=[{"slug": "long-gone", "note": "404 everywhere"}],
    )
    slugs = [org.slug for _, org in companies.active()]

    assert "wealth-com" in slugs
    assert "long-gone" not in slugs


def test_company_display_name_falls_back_to_the_slug():
    companies = CompaniesConfig(ashby=[{"slug": "xbowcareers"}])
    assert companies.ashby[0].display_name == "xbowcareers"
