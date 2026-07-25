"""The registry: company to ATS vendor to slug to live-or-dead.

This is the asset. There is no single Greenhouse API, only one endpoint per
company addressed by a slug that is frequently not the company name. The engine
read 8 orgs for months because Eric knew 8 slugs. This file is how it stops
being limited by what one person happens to know.

Persistence is data/registry.jsonl, one record per line, keyed by
(vendor, slug). It is generated data, never hand-edited config, and it lives
under data/ where the hygiene guard already guarantees it can never be
committed.

Self-healing, never destructive: a slug that stops answering becomes status
dead with a dated note, and a dead slug that answers again flips back to live
with a note. No row is ever deleted. The history of a board's disappearances
is churn data, and chainalysis-careers already proved a "dead" verdict can be
half wrong.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from engine.config import data_dir

Vendor = Literal[
    "ashby",
    "greenhouse",
    "lever",
    "workable",
    "smartrecruiters",
    "workday",
    "unknown",
]
OrgStatus = Literal["live", "dead", "unverified"]

VENDORS: tuple[str, ...] = (
    "ashby",
    "greenhouse",
    "lever",
    "workable",
    "smartrecruiters",
    "workday",
)


class OrgRecord(BaseModel):
    """One company's board on one ATS.

    vendor "unknown" is a real outcome, not an error: it records that a domain
    was scanned and no board pattern matched, which is what makes a discovery
    pass over thousands of domains resumable instead of Groundhog Day.
    """

    company_name: str = ""
    domain: str = ""
    vendor: Vendor = "unknown"
    slug: str = ""
    board_url: str = ""
    status: OrgStatus = "unverified"
    last_verified: date | None = None
    last_posting_count: int = 0
    discovered_via: str = ""
    notes: str = ""

    @property
    def key(self) -> tuple[str, str]:
        return (self.vendor, self.slug)

    def add_note(self, line: str) -> None:
        self.notes = f"{self.notes}; {line}" if self.notes else line


class RegistryStats(BaseModel):
    total: int = 0
    live_by_vendor: dict[str, int] = Field(default_factory=dict)
    live: int = 0
    dead: int = 0
    unverified: int = 0
    no_ats_found: int = 0
    postings_last_seen: int = 0


class Registry:
    """Append-or-update by (vendor, slug). Never deletes a row."""

    def __init__(self, path: Path):
        self.path = path
        self.records: dict[tuple[str, str], OrgRecord] = {}

    @classmethod
    def load(cls, path: Path) -> Registry:
        reg = cls(path)
        if not path.is_file():
            return reg
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                record = OrgRecord(**json.loads(line))
            except (ValueError, TypeError):
                # One mangled line must not take down two thousand good ones.
                continue
            reg.records[record.key] = record
        return reg

    def save(self) -> None:
        """Write-then-rename, so an interrupted save never truncates the registry."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".jsonl.tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            for record in sorted(self.records.values(), key=lambda r: r.key):
                fh.write(record.model_dump_json() + "\n")
        tmp.replace(self.path)

    def get(self, vendor: str, slug: str) -> OrgRecord | None:
        return self.records.get((vendor, slug))

    def upsert(self, candidate: OrgRecord) -> OrgRecord:
        """New key, new row. Existing key, fill the blanks and keep the history.

        A rediscovery must never overwrite status, notes, or verification
        history: those are exactly the fields the registry exists to accumulate.
        """
        existing = self.records.get(candidate.key)
        if existing is None:
            self.records[candidate.key] = candidate
            return candidate
        for field in ("company_name", "domain", "board_url", "discovered_via"):
            if getattr(candidate, field) and not getattr(existing, field):
                setattr(existing, field, getattr(candidate, field))
        return existing

    def mark_live(
        self, vendor: str, slug: str, posting_count: int, when: date, company_name: str = ""
    ) -> OrgRecord:
        record = self.records[(vendor, slug)]
        if record.status == "dead":
            record.add_note(
                f"back from the dead on {when.isoformat()} with {posting_count} postings"
            )
        record.status = "live"
        record.last_verified = when
        record.last_posting_count = posting_count
        if company_name:
            # The vendor's own API knows the real name better than a domain guess.
            record.company_name = company_name
        return record

    def mark_dead(self, vendor: str, slug: str, when: date, reason: str) -> OrgRecord:
        record = self.records[(vendor, slug)]
        if record.status != "dead":
            record.add_note(f"went dead on {when.isoformat()}: {reason}")
        record.status = "dead"
        record.last_verified = when
        record.last_posting_count = 0
        return record

    def live(self, vendor: str | None = None) -> list[OrgRecord]:
        return [
            r
            for r in sorted(self.records.values(), key=lambda r: r.key)
            if r.status == "live"
            and r.vendor != "unknown"
            and (vendor is None or r.vendor == vendor)
        ]

    def domains_seen(self) -> set[str]:
        """Every domain any record claims, including the no-match markers.

        This is what makes add-domains resumable: a domain in this set has
        already been scanned and is skipped on the next pass.
        """
        return {r.domain for r in self.records.values() if r.domain}

    def stats(self) -> RegistryStats:
        stats = RegistryStats(total=len(self.records))
        for record in self.records.values():
            if record.vendor == "unknown":
                stats.no_ats_found += 1
                continue
            if record.status == "live":
                stats.live += 1
                stats.live_by_vendor[record.vendor] = (
                    stats.live_by_vendor.get(record.vendor, 0) + 1
                )
                stats.postings_last_seen += record.last_posting_count
            elif record.status == "dead":
                stats.dead += 1
            else:
                stats.unverified += 1
        return stats


def registry_path(root: Path | None = None) -> Path:
    return data_dir(root) / "registry.jsonl"
