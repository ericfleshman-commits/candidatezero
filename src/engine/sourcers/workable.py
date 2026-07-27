"""Workable widget accounts API.

GET https://apply.workable.com/api/v1/widget/accounts/{slug}?details=true

Shape verified live on 2026-07-26 against the CrewAI and Dreamdata boards:
one unauthenticated request returns {name, description, jobs: [...]}, and
with details=true each job carries its full HTML description inline. That
makes Workable an Ashby-class citizen: no per-role detail fetch, ever.

What a job does NOT carry is a structured salary band. There is no comp
field anywhere in the public payload, so pay only exists when the org wrote
it into the description prose, and it is parsed from there and recorded as
comp_source "parsed" like any other inference.

Remote is the "telecommuting" boolean. Locations arrive twice: flat
city/state/country fields for the primary, plus a locations[] list when the
posting has several. The list wins when present.
"""

from __future__ import annotations

from datetime import datetime

from engine.comp import html_to_text, parse_salary_text
from engine.config import CompanyEntry
from engine.models import CompRange, Role
from engine.sourcers.base import DetailPredicate, PoliteClient

ACCOUNT_URL = "https://apply.workable.com/api/v1/widget/accounts/{slug}?details=true"


def _parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _segment(city: str | None, region: str | None, country: str | None) -> str:
    return ", ".join(p.strip() for p in (city, region, country) if p and p.strip())


def _locations(job: dict) -> str:
    """locations[] entries pipe-joined; the flat fields cover the single-site case."""
    parts: list[str] = []
    for loc in job.get("locations") or []:
        seg = _segment(loc.get("city"), loc.get("region"), loc.get("country"))
        if seg and seg not in parts:
            parts.append(seg)
    if not parts:
        seg = _segment(job.get("city"), job.get("state"), job.get("country"))
        if seg:
            parts.append(seg)
    return " | ".join(parts)


class WorkableSourcer:
    source = "workable"

    def __init__(self, client: PoliteClient):
        self.client = client

    def fetch(self, org: CompanyEntry, needs_detail: DetailPredicate | None = None) -> list[Role]:
        payload = self.client.get_json(ACCOUNT_URL.format(slug=org.slug))
        company = payload.get("name") or org.display_name
        roles: list[Role] = []

        for job in payload.get("jobs") or []:
            description = html_to_text(job.get("description"))
            parsed = parse_salary_text(description)
            if parsed.min is not None or parsed.max is not None:
                comp = CompRange(
                    min=parsed.min, max=parsed.max, currency=parsed.currency, source="parsed"
                )
                comp_flags = parsed.flags
            else:
                comp, comp_flags = CompRange(source="none"), []

            role = Role(
                id=Role.make_id(self.source, org.slug, job.get("shortcode", "")),
                source=self.source,
                org_slug=org.slug,
                company_name=company,
                title=(job.get("title") or "").strip(),
                location_raw=_locations(job),
                is_remote=job.get("telecommuting"),
                comp=comp,
                url=job.get("url") or job.get("application_url") or "",
                published_at=_parse_date(job.get("published_on")),
                description_text=description,
            )
            for flag in comp_flags:
                role.flag(flag)
            roles.append(role)

        return roles
