"""Liveness re-check.

Tonight this module looks like theatre. Every role we filter came from the board
API seconds earlier, so of course it is live. The contract is built now because
of what is coming:

- Night 4 brings board scrapers (Built In, Clay, GTMEcareers). Those roles arrive
  UNVERIFIED and are frequently zombies: reposted, filled, or never real.
- The seen-store re-checks roles first seen days ago. That is where roles quietly
  die, and noticing that is churn data nobody publishes.

"Verified live on the employer's own system" is the entire promise of this tool.
This is the module that has to make it true, so it gets a real contract now
rather than a retrofit later.
"""

from __future__ import annotations

from datetime import UTC, datetime

from engine.models import LivenessCheck, Role
from engine.sourcers.base import OrgUnavailable, PoliteClient


def _now() -> datetime:
    return datetime.now(UTC)


def verify(
    role: Role,
    client: PoliteClient,
    board_ids: set[str] | None = None,
) -> LivenessCheck:
    """Is this posting still real?

    Two checks, cheapest first:
      a. the role id is still present on the org's board endpoint
      b. the posting URL answers 200

    A missing req or a dead link means zombie: drop it and count it.
    """
    if board_ids is not None and role.id not in board_ids:
        return LivenessCheck(
            live=False,
            checked_at=_now(),
            method="board-presence",
            detail="req is no longer listed on the org board",
        )

    if not role.url:
        # Nothing to probe. Trust board presence rather than invent a failure.
        return LivenessCheck(
            live=True,
            checked_at=_now(),
            method="board-presence",
            detail="no posting url to probe",
        )

    try:
        resp = client.head(role.url)
        if resp.status_code >= 400:
            # Plenty of boards refuse HEAD but answer GET. Do not call a role dead
            # over an HTTP method preference.
            resp = client.get(role.url)
        live = resp.status_code < 400
        return LivenessCheck(
            live=live,
            checked_at=_now(),
            method="url-probe",
            detail=f"HTTP {resp.status_code}",
        )
    except OrgUnavailable as exc:
        # A timeout is not proof of death. Do not throw away a real role because
        # someone's CDN blinked at 3am.
        return LivenessCheck(
            live=True,
            checked_at=_now(),
            method="url-probe-inconclusive",
            detail=str(exc),
        )
