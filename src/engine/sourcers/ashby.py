"""Ashby posting API.

GET https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true

The good ATS, from this engine's point of view: one unauthenticated request
returns every posting for an org, with a structured salary band and the full job
description text already in it. No detail fetch, no scraping, no guessing.
"""

from __future__ import annotations

from datetime import datetime

from engine.comp import annualize, parse_salary_text
from engine.config import CompanyEntry
from engine.models import CompRange, Role
from engine.sourcers.base import DetailPredicate, PoliteClient

BOARD_URL = "https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true"


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _locations(job: dict) -> str:
    """Flatten primary and secondary locations into one pipe-separated string.

    Greenhouse already publishes locations pipe-separated, so matching that shape
    here means the location filter has exactly one format to understand.
    """
    parts: list[str] = []
    primary = (job.get("location") or "").strip()
    if primary:
        parts.append(primary)
    for secondary in job.get("secondaryLocations") or []:
        name = (secondary.get("location") or "").strip()
        if name and name not in parts:
            parts.append(name)
    return " | ".join(parts)


def extract_comp(job: dict) -> tuple[CompRange, list[str]]:
    """Structured band first, prose summary second, nothing third.

    The plan says to use the salary component of the first tier. In practice a
    tier can carry only equity or commission, so this takes the first Salary
    component found in tier order: identical when the first tier has one, and it
    still finds the band when it does not.
    """
    compensation = job.get("compensation") or {}

    for tier in compensation.get("compensationTiers") or []:
        for component in tier.get("components") or []:
            if component.get("compensationType") != "Salary":
                continue
            low, scaled_low = annualize(component.get("minValue"), component.get("interval"))
            high, scaled_high = annualize(component.get("maxValue"), component.get("interval"))
            if low is None and high is None:
                continue
            flags: list[str] = []
            if scaled_low or scaled_high:
                flags.append("comp-hourly-annualized")
            return (
                CompRange(
                    min=low,
                    max=high,
                    currency=component.get("currencyCode") or "USD",
                    source="structured",
                ),
                flags,
            )

    # Some orgs publish only the human-readable string.
    summary = compensation.get("scrapeableCompensationSalarySummary") or compensation.get(
        "compensationTierSummary"
    )
    parsed = parse_salary_text(summary)
    if parsed.min is not None or parsed.max is not None:
        return (
            CompRange(min=parsed.min, max=parsed.max, currency=parsed.currency, source="parsed"),
            parsed.flags,
        )

    return CompRange(source="none"), []


class AshbySourcer:
    source = "ashby"

    def __init__(self, client: PoliteClient):
        self.client = client

    def fetch(self, org: CompanyEntry, needs_detail: DetailPredicate | None = None) -> list[Role]:
        payload = self.client.get_json(BOARD_URL.format(slug=org.slug))
        roles: list[Role] = []

        for job in payload.get("jobs") or []:
            # Unlisted postings are drafts, internal, or closed. Not ours to see.
            if not job.get("isListed"):
                continue

            comp, comp_flags = extract_comp(job)
            role = Role(
                id=Role.make_id(self.source, org.slug, job.get("id", "")),
                source=self.source,
                org_slug=org.slug,
                company_name=org.display_name,
                title=(job.get("title") or "").strip(),
                location_raw=_locations(job),
                is_remote=job.get("isRemote"),
                comp=comp,
                url=job.get("jobUrl") or job.get("applyUrl") or "",
                published_at=_parse_dt(job.get("publishedAt")),
                updated_at=_parse_dt(job.get("updatedAt")),
                description_text=job.get("descriptionPlain"),
            )
            for flag in comp_flags:
                role.flag(flag)
            if (job.get("workplaceType") or "").lower() == "remote" and role.is_remote is None:
                role.is_remote = True
            roles.append(role)

        return roles
