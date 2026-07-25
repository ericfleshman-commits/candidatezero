"""The Sourcer contract, and the manners every sourcer inherits.

We are a guest on other people's infrastructure. That means an honest User-Agent
that says who we are, a real timeout, a deliberate pause between requests to the
same host, and a dead org that lands in the digest footer instead of taking the
whole run down at 3am.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Protocol

import httpx

from engine.config import HOST_DELAY_SECONDS, REQUEST_TIMEOUT, USER_AGENT, CompanyEntry
from engine.models import Role

DetailPredicate = Callable[[Role], bool]

# Statuses worth retrying with backoff. A 429 is the host asking us to slow
# down, and a 5xx is the host having a bad moment. Everything else is an answer.
RETRY_STATUSES = {429, 500, 502, 503, 504}
MAX_ATTEMPTS = 3


class OrgNotFound(Exception):
    """The board 404ed. The slug is wrong or the org left this ATS."""


class OrgUnavailable(Exception):
    """The board failed in a way that is not a clean 404: timeout, 5xx, bad JSON."""


class PoliteClient:
    """An httpx wrapper that waits its turn.

    The delay is per host, not global, so probing Ashby does not slow down
    Greenhouse. Set delay to 0 in tests.

    Safe to share across threads: the registry's discovery pass runs four
    workers, and the per-host pacing has to hold across all of them, not per
    worker. Each caller reserves its slot under a lock and sleeps outside it.

    A 429 or 5xx is retried with exponential backoff. The backoff scales off
    the politeness delay, so a test client with delay=0 retries instantly and
    the production client waits 1s, then 2s.
    """

    def __init__(self, client: httpx.Client | None = None, delay: float = HOST_DELAY_SECONDS):
        self._client = client or httpx.Client(
            timeout=REQUEST_TIMEOUT,
            headers={"User-Agent": USER_AGENT},
            follow_redirects=True,
        )
        self._delay = delay
        self._last_hit: dict[str, float] = {}
        self._lock = threading.Lock()

    def _wait_turn(self, url: str) -> None:
        host = httpx.URL(url).host
        with self._lock:
            now = time.monotonic()
            last = self._last_hit.get(host)
            ready = now if last is None else max(now, last + self._delay)
            self._last_hit[host] = ready
        if ready > now:
            time.sleep(ready - now)

    def _request(self, method: str, url: str, **kwargs) -> httpx.Response:
        for attempt in range(MAX_ATTEMPTS):
            self._wait_turn(url)
            try:
                resp = self._client.request(method, url, **kwargs)
            except httpx.HTTPError as exc:
                raise OrgUnavailable(f"{type(exc).__name__}: {exc}") from exc
            if resp.status_code in RETRY_STATUSES and attempt < MAX_ATTEMPTS - 1:
                time.sleep(self._delay * 2 * (2**attempt))
                continue
            return resp
        raise OrgUnavailable(f"retries exhausted at {url}")  # pragma: no cover

    def get(self, url: str, **kwargs) -> httpx.Response:
        return self._request("GET", url, **kwargs)

    def head(self, url: str, **kwargs) -> httpx.Response:
        """Cheapest possible liveness probe: ask for the headers, not the page."""
        self._wait_turn(url)
        try:
            return self._client.head(url, **kwargs)
        except httpx.HTTPError as exc:
            raise OrgUnavailable(f"{type(exc).__name__}: {exc}") from exc

    def get_json(self, url: str, **kwargs) -> dict:
        return self._json(self.get(url, **kwargs), url)

    def post_json(self, url: str, **kwargs) -> dict:
        """Workday's public job search only answers POST. Same manners apply."""
        return self._json(self._request("POST", url, **kwargs), url)

    @staticmethod
    def _json(resp: httpx.Response, url: str) -> dict:
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
