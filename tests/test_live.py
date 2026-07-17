"""Live smoke tests. Excluded by default.

    uv run pytest -m live

These hit real boards, so they are slow, they are rude if you loop them, and they
will break when a company changes ATS. That is not a flaky test, that is the
signal this whole engine exists to catch. When one fails, run check-org.
"""

from __future__ import annotations

import pytest

from engine.config import CompanyEntry
from engine.pipeline import check_org
from engine.sourcers.ashby import AshbySourcer
from engine.sourcers.base import PoliteClient
from engine.sourcers.greenhouse import GreenhouseSourcer

pytestmark = pytest.mark.live


@pytest.fixture
def live_client() -> PoliteClient:
    c = PoliteClient()
    yield c
    c.close()


def test_ashby_wealth_com_is_still_there(live_client):
    roles = AshbySourcer(live_client).fetch(CompanyEntry(slug="wealth-com", company="Wealth.com"))

    assert roles, "wealth-com returned no listed roles"
    assert all(r.source == "ashby" for r in roles)
    assert all(r.url.startswith("https://jobs.ashbyhq.com/wealth-com/") for r in roles)
    # Ashby's whole advantage is that pay is structured and already in the board.
    assert any(r.comp.source == "structured" for r in roles)


def test_greenhouse_gongio_is_still_there(live_client):
    sourcer = GreenhouseSourcer(live_client)
    roles = sourcer.fetch(CompanyEntry(slug="gongio", company="Gong"), needs_detail=lambda r: False)

    assert roles, "gongio returned no roles"
    assert any("|" in r.location_raw for r in roles), "expected a multi-location posting"
    # No detail predicate means no detail fetches. Cheap first.
    assert sourcer.detail_fetches == 0


def test_check_org_disagrees_with_the_two_atss(live_client):
    findings = {f["source"]: f for f in check_org("gongio", live_client)}

    assert findings["greenhouse"]["status"] == "ok"
    assert findings["ashby"]["status"] == "404"
