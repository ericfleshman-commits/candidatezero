"""The Sourcer contract, and the manners every sourcer inherits.

We are a guest on other people's infrastructure. That means an honest User-Agent
that says who we are, a real timeout, a deliberate pause between requests to the
same host, and a dead org that lands in the digest footer instead of taking the
whole run down at 3am.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Protocol

import httpx

from engine.config import HOST_DELAY_SECONDS, REQUEST_TIMEOUT, USER_AGENT, CompanyEntry
from engine.models import Role

DetailPredicate = Callable[[Role], bool]


class OrgNotFound(Exception):
    """The board 404ed. The slug is wrong or the org left this ATS."""


class OrgUnavailable(Exception):
    """The board failed in a way that is not a clean 404: timeout, 5xx, bad JSON."""


class PoliteClient:
    """An httpx wrapper that waits its turn.

    The delay is per host, not global, so probing Ashby does not slow down
    Greenhouse. Set delay to 0 in tests.
    """

    def __init__(self, client: httpx.Client | None = None, delay: float = HOST_DELAY_SECONDS):
        self._client = client or httpx.Client(
            timeout=REQUEST_TIMEOUT,
            headers={"User-Agent": USER_AGENT},
            follow_redirects=True,
        )
        self._delay = delay
        self._last_hit: dict[str, float] = {}

    def _wait_turn(self, url: str) -> None:
        host = httpx.URL(url).host
        last = self._last_hit.get(host)
        if last is not None:
            elapsed = time.monotonic() - last
            if elapsed < self._delay:
                time.sleep(self._delay - elapsed)
        self._last_hit[host] = time.monotonic()

    def get(self, url: str, **kwargs) -> httpx.Response:
        self._wait_turn(url)
        try:
            return self._client.get(url, **kwargs)
        except httpx.HTTPError as exc:
            raise OrgUnavailable(f"{type(exc).__name__}: {exc}") from exc

    def head(self, url: str, **kwargs) -> httpx.Response:
        """Cheapest possible liveness probe: ask for the headers, not the page."""
        self._wait_turn(url)
        try:
            return self._client.head(url, **kwargs)
        except httpx.HTTPError as exc:
            raise OrgUnavailable(f"{type(exc).__name__}: {exc}") from exc

    def get_json(self, url: str, **kwargs) -> dict:
        resp = self.get(url, **kwargs)
        if resp.status_code == 404:
            raise OrgNotFound(f"404 at {url}")
        if resp.status_code >= 400:
            raise OrgUnavailable(f"HTTP {resp.status_code} at {url}")
        try:
            return resp.json()
        except ValueError as exc:
            raise OrgUnavailable(f"Bad JSON at {url}: {exc}") from exc

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> PoliteClient:
        return self

    def __exit__(self, *exc) -> None:
        self.close()


class Sourcer(Protocol):
    """fetch(org) returns the org's live postings, normalized to Role.

    needs_detail lets a sourcer skip expensive per-role work for roles the
    pipeline has already decided it does not want. Ashby ignores it because its
    board response is complete. Greenhouse leans on it hard.
    """

    source: str

    def fetch(self, org: CompanyEntry, needs_detail: DetailPredicate | None = None) -> list[Role]:
        ...
