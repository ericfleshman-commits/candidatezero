"""Liveness verification.

Redundant tonight, load-bearing on Night 4 when scraped roles arrive unverified.
"""

from __future__ import annotations

import httpx
import respx
from conftest import make_role

from engine.verify import verify

URL = "https://example.com/job/1"


@respx.mock
def test_a_posting_that_answers_200_is_live(client):
    respx.head(URL).mock(return_value=httpx.Response(200))
    check = verify(make_role(), client)

    assert check.live is True
    assert check.method == "url-probe"


@respx.mock
def test_a_dead_link_is_a_zombie(client):
    respx.head(URL).mock(return_value=httpx.Response(404))
    respx.get(URL).mock(return_value=httpx.Response(404))

    assert verify(make_role(), client).live is False


@respx.mock
def test_head_refusal_falls_back_to_get(client):
    """Plenty of boards refuse HEAD. That is a method preference, not a death."""
    respx.head(URL).mock(return_value=httpx.Response(405))
    respx.get(URL).mock(return_value=httpx.Response(200))

    assert verify(make_role(), client).live is True


def test_a_role_missing_from_the_board_is_a_zombie(client):
    """The req is gone from the org's board. No probe needed."""
    check = verify(make_role(), client, board_ids={"ashby:acme:999"})

    assert check.live is False
    assert check.method == "board-presence"


@respx.mock
def test_a_role_present_on_the_board_is_still_probed(client):
    respx.head(URL).mock(return_value=httpx.Response(200))
    check = verify(make_role(), client, board_ids={"ashby:acme:1"})

    assert check.live is True
    assert check.method == "url-probe"


@respx.mock
def test_a_timeout_is_not_proof_of_death(client):
    """Do not throw away a real role because someone's CDN blinked at 3am."""
    respx.head(URL).mock(side_effect=httpx.ConnectTimeout("timed out"))
    check = verify(make_role(), client)

    assert check.live is True
    assert check.method == "url-probe-inconclusive"


def test_a_role_with_no_url_is_trusted_on_board_presence(client):
    check = verify(make_role(url=""), client)

    assert check.live is True
    assert check.method == "board-presence"
