"""The public listing store: data/public-roles.jsonl.

This is the board's half of the privacy firewall, and like the newsletter's it
is structural. The pipeline feeds this store immediately after the title-family
match and before any private filtering, so nothing downstream of that point,
not the comp floor, not the location rules, not history dedupe, not a single
flag, can influence what is in here. The firewall is the record shape itself:
PublicRole has no field a private rule could travel in.

What each record carries is the employer's own public data plus the engine's
sighting dates: vendor, company, title, the band when the employer publishes
one, location, remote signal, apply URL, posted date, first_seen, last_seen,
last_verified. A role that vanishes from a board that answered gets closed_at,
stays listed for CLOSED_RETENTION_DAYS as churn data, then drops from the file.

Because it is public by construction, this file is safe to publish or copy
anywhere, even though data/ as a whole stays gitignored.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from pydantic import BaseModel

from engine.dedupe import normalize_company
from engine.filters import normalize_title
from engine.models import Role

# A closed role is churn content for a week, then it is gone. Nobody needs a
# graveyard, they need to know what closed recently.
CLOSED_RETENTION_DAYS = 7


class PublicRole(BaseModel):
    """One publicly listable posting. Every field is either the employer's own
    published data or a date the engine observed. Nothing else may be added
    here without deciding, out loud, that it is public."""

    id: str  # vendor:slug:external_id, the upsert key
    vendor: str
    org_key: str  # vendor:slug, matched against the orgs that answered tonight
    company: str
    title: str
    comp_band: str = ""  # employer-published band only; empty means not published
    location: str = ""
    remote: bool = False
    url: str = ""
    posted_at: datetime | None = None
    first_seen: datetime
    last_seen: datetime
    # When this role was last confirmed present on the employer's own ATS.
    # Tonight that is the fetch itself: the engine reads the employer's board
    # directly, so appearing in a run is the verification.
    last_verified: datetime
    closed_at: datetime | None = None
    # Set when this record is a cross-vendor mirror of an earlier posting.
    # Collapsed records stay in the file (they are still sightings) but the
    # board lists only the record they collapsed into.
    collapsed_into: str | None = None


def _posted_key(entry: PublicRole) -> datetime:
    """Earliest-posted ordering. Vendors that publish only a date hand back a
    naive datetime; treat it as UTC so it can be compared with our stamps."""
    when = entry.posted_at or entry.first_seen
    return when if when.tzinfo is not None else when.replace(tzinfo=UTC)


def _from_role(role: Role, now: datetime) -> PublicRole:
    return PublicRole(
        id=role.id,
        vendor=role.source,
        org_key=f"{role.source}:{role.org_slug}",
        company=role.company_name,
        title=role.title,
        comp_band=role.comp.human() if role.comp.known else "",
        location=role.location_raw or "",
        remote=bool(role.is_remote),
        url=role.url,
        posted_at=role.published_at,
        first_seen=now,
        last_seen=now,
        last_verified=now,
    )


class PublicStore:
    """Load, reconcile, save. One JSON line per role, rewritten each run."""

    def __init__(self, roles: dict[str, PublicRole] | None = None):
        self.roles: dict[str, PublicRole] = roles or {}

    @classmethod
    def load(cls, path: Path) -> PublicStore:
        store = cls()
        if not path.is_file():
            return store
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                record = PublicRole.model_validate_json(line)
            except ValueError:
                # One mangled line must not cost the store its history.
                continue
            store.roles[record.id] = record
        return store

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = [self.roles[key].model_dump_json() for key in sorted(self.roles)]
        path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")

    def open_roles(self) -> list[PublicRole]:
        """Live, listable roles: not closed, not a collapsed mirror."""
        return [
            r for r in self.roles.values() if r.closed_at is None and r.collapsed_into is None
        ]

    def closed_roles(self) -> list[PublicRole]:
        """Closed but still within the retention window, mirrors excluded."""
        return [
            r for r in self.roles.values() if r.closed_at is not None and r.collapsed_into is None
        ]

    def reconcile(
        self, roles: list[Role], scanned_org_keys: set[str], now: datetime
    ) -> None:
        """Fold tonight's title-family-matched roles into the store.

        Same closure rule as the seen-store: a role only closes when its org
        actually answered tonight. A board that 404s or times out proves
        nothing about its roles.
        """
        current_ids = {r.id for r in roles}

        for role in roles:
            entry = self.roles.get(role.id)
            if entry is None:
                self.roles[role.id] = _from_role(role, now)
                continue
            fresh = _from_role(role, now)
            fresh.first_seen = entry.first_seen
            self.roles[role.id] = fresh  # a repost is a resurrection: closed_at resets

        for role_id, entry in list(self.roles.items()):
            if role_id in current_ids:
                continue
            if entry.closed_at is None:
                if entry.org_key in scanned_org_keys:
                    entry.closed_at = now
            elif now - entry.closed_at > timedelta(days=CLOSED_RETENTION_DAYS):
                del self.roles[role_id]

        self._collapse_duplicates()

    def _collapse_duplicates(self) -> None:
        """Same company, same title, posted on more than one vendor: keep the
        earliest-posted and mark the mirrors collapsed.

        Only cross-vendor mirrors collapse. Two same-title postings on the same
        vendor are distinct requisitions (usually different locations) and both
        stay listed. Recomputed from scratch each run so a keeper that closes
        hands the listing back to a surviving mirror.
        """
        groups: dict[tuple[str, str], list[PublicRole]] = {}
        for entry in self.roles.values():
            if entry.closed_at is not None:
                continue
            entry.collapsed_into = None
            key = (normalize_company(entry.company), normalize_title(entry.title))
            groups.setdefault(key, []).append(entry)

        for members in groups.values():
            if len({m.vendor for m in members}) < 2:
                continue
            members.sort(key=lambda m: (_posted_key(m), m.id))
            keeper = members[0]
            for mirror in members[1:]:
                if mirror.vendor != keeper.vendor:
                    mirror.collapsed_into = keeper.id
