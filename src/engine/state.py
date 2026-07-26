"""The seen-store: data/seen.json.

Keyed by Role.id, which is namespaced ats:org:external_id precisely so two ATSs
handing out the same integer cannot collide here.

This is what turns a nightly list into a diff. It answers "what is new tonight"
and, more interestingly, "what disappeared". A role that vanishes from a board
was filled, pulled, or was never real. Nobody publishes that churn number. It
becomes content later.

The store tracks every role the board showed us, not just the ones that survived
filtering. Otherwise a role that dips below the comp floor would look like it
closed, which would be a lie.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, Field

from engine.models import Role


class SeenEntry(BaseModel):
    org_key: str
    title: str = ""
    company_name: str = ""
    url: str = ""
    first_seen: datetime
    last_seen: datetime
    # Set when the role vanishes from a board that answered. The weekly report
    # publishes this churn, and the same mechanism is the future per-user
    # "closure by design" feature: telling a person the role they applied to is
    # gone, so the silence they are getting stops being about them.
    closed_at: datetime | None = None
    # Public-report fields, set only for roles that cleared every filter and
    # the liveness probe with no flags. comp_band is the employer's own
    # published band, already rendered for humans. The store never records WHY
    # anything was flagged or suppressed; those reasons stay private.
    public: bool = False
    comp_band: str = ""
    location: str = ""


class SeenStore(BaseModel):
    version: int = 1
    roles: dict[str, SeenEntry] = Field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> SeenStore:
        if not path.is_file():
            return cls()
        try:
            return cls(**json.loads(path.read_text(encoding="utf-8")))
        except (ValueError, TypeError):
            # A corrupt store is not worth crashing a 3am run over. Start fresh;
            # the worst case is one night where everything looks new.
            return cls()

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.model_dump_json(indent=2), encoding="utf-8")

    def reconcile(
        self,
        roles: list[Role],
        scanned_org_keys: set[str],
        now: datetime | None = None,
    ) -> tuple[set[str], list[SeenEntry]]:
        """Fold tonight's board into the store.

        Returns the ids never seen before, and the entries that have gone away.

        Closure is only ever inferred for orgs that actually answered tonight. If
        a board 404s or times out, its roles are missing from this run, and
        calling them closed would turn one flaky request into a wall of fake
        CLOSED lines in the digest.
        """
        stamp = now or datetime.now(UTC)
        current_ids = {r.id for r in roles}
        new_ids: set[str] = set()

        for role in roles:
            entry = self.roles.get(role.id)
            if entry is None:
                new_ids.add(role.id)
                self.roles[role.id] = SeenEntry(
                    org_key=f"{role.source}:{role.org_slug}",
                    title=role.title,
                    company_name=role.company_name,
                    url=role.url,
                    first_seen=stamp,
                    last_seen=stamp,
                )
            else:
                entry.last_seen = stamp
                entry.closed_at = None  # A repost is a resurrection.
                entry.title = role.title or entry.title
                entry.url = role.url or entry.url

        closed: list[SeenEntry] = []
        for role_id, entry in self.roles.items():
            if role_id in current_ids or entry.closed_at is not None:
                continue
            if entry.org_key not in scanned_org_keys:
                continue  # Org did not answer tonight. Absence proves nothing.
            entry.closed_at = stamp
            closed.append(entry)

        return new_ids, closed

    def record_public(self, roles: list[Role]) -> None:
        """Mark roles fit for the public weekly report.

        Only clean survivors belong here. A flagged role is refused even if a
        caller passes one, because flags encode the operator's private rules
        and nothing on the store's public side may depend on them.
        """
        for role in roles:
            if role.flags:
                continue
            entry = self.roles.get(role.id)
            if entry is None:
                continue
            entry.public = True
            entry.comp_band = role.comp.human()
            location = role.location_raw or "not stated"
            if role.is_remote and "remote" not in location.lower():
                location += " (remote)"
            entry.location = location
