"""Shared test fixtures.

Every payload under tests/fixtures/ is a real response recorded from a live board
on 2026-07-16, trimmed and de-styled. No test in this suite touches the network.
The one that does is marked live and excluded by default.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from engine.config import CompaniesConfig, CompanyEntry, Config, FiltersConfig
from engine.models import CompRange, Role
from engine.sourcers.base import PoliteClient

FIXTURES = Path(__file__).parent / "fixtures"
REPO_ROOT = Path(__file__).resolve().parents[1]


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture
def ashby_board() -> dict:
    return load_fixture("ashby_board.json")


@pytest.fixture
def greenhouse_list() -> dict:
    return load_fixture("greenhouse_list.json")


@pytest.fixture
def greenhouse_detail() -> dict:
    return load_fixture("greenhouse_detail.json")


@pytest.fixture
def client() -> PoliteClient:
    # delay=0 so the politeness pause does not make the suite crawl.
    c = PoliteClient(delay=0)
    yield c
    c.close()


@pytest.fixture
def filters_cfg() -> FiltersConfig:
    """The real shipped example config, so the tests guard what users actually get."""
    raw = yaml.safe_load((REPO_ROOT / "config" / "example" / "filters.yaml").read_text())
    return FiltersConfig(**raw)


@pytest.fixture
def config(filters_cfg: FiltersConfig) -> Config:
    return Config(
        companies=CompaniesConfig(
            ashby=[CompanyEntry(slug="wealth-com", company="Wealth.com")],
            greenhouse=[CompanyEntry(slug="gongio", company="Gong")],
        ),
        filters=filters_cfg,
    )


def make_role(**kwargs) -> Role:
    """A Role with sane defaults, so each test only states what it cares about."""
    base = dict(
        id="ashby:acme:1",
        source="ashby",
        org_slug="acme",
        company_name="Acme",
        title="GTM Engineer",
        location_raw="New York City",
        url="https://example.com/job/1",
    )
    comp = kwargs.pop("comp", None)
    base.update(kwargs)
    role = Role(**base)
    if comp is not None:
        role.comp = comp if isinstance(comp, CompRange) else CompRange(**comp)
    return role
