"""Lever postings API.

GET https://api.lever.co/v0/postings/{slug}?mode=json

One unauthenticated request returns every published posting as a JSON list,
not an object. Recorded live on 2026-07-25: an unknown slug answers 404 with
an {"ok": false} body, and a valid org with nothing published answers 200 with
an empty list. Those are different facts: the first is a dead board, the
second is a quiet one.

Pay arrives two ways. Some orgs publish a structured salaryRange with an
interval string like "per-year-salary". The rest bury the band in prose, most
often in the "additional" closing block rather than the description, so the
parser reads both.
"""

from __future__ import annotations

from datetime import UTC, datetime

from engine.comp import annualize, html_to_text, parse_salary_text
from engine.config import CompanyEntry
from engine.models import CompRange, Role
from engine.sourcers.base import DetailPredicate, PoliteClient

POSTINGS_URL = "https://api.lever.co/v0/postings/{slug}?mode=json"

# Lever interval strings, mapped onto the annualizer's vocabulary. Anything
# unrecognized is assumed annual, same as everywhere else in this engine.
_INTERVALS = {
    "hour": "1 HOUR",
    "day": "1 DAY",
    "week": "1 WEEK",
    "month": "1 MONTH",
    "year": "1 YEAR",
}


def _interval(raw: str | None) -> str | None:
    low = (raw or "").lower()
    for token, canonical in _INTERVALS.items():
        if token in low:
            return canonical
    return None


def _parse_ms(value: int | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromtimestamp(value / 1000, tz=UTC)


def _locations(job: dict) -> str:
    """Primary location plus allLocations, pipe-joined like every other sourcer."""
    categories = job.get("categories") or {}
    parts: list[str] = []
    primary = (categories.get("location") or "").strip()
    if primary:
        parts.append(primary)
    for name in categories.get("allLocations") or []:
        name = (name or "").strip()
        if name and name not in parts:
            parts.append(name)
    return " | ".join(parts)


def extract_comp(job: dict) -> tuple[CompRange, list[str]]:
    """Structured salaryRange first, prose second, nothing third."""
    salary = job.get("salaryRange") or {}
    interval = _interval(salary.get("interval"))
    low, scaled_low = annualize(salary.get("min"), interval)
    high, scaled_high = annualize(salary.get("max"), interval)
    if low is not None or high is not None:
        flags = ["comp-hourly-annualized"] if scaled_low or scaled_high else []
        return (
            CompRange(
                min=low,
                max=high,
                currency=salary.get("currency") or "USD",
                source="structured",
            ),
            flags,
        )

    prose = "\n".join(
        part
        for part in (
            job.get("descriptionPlain"),
            job.get("additionalPlain"),
            *(html_to_text(item.get("content")) for item in job.get("lists") or []),
        )
        if part
    )
    parsed = parse_salary_text(prose)
    if parsed.min is not None or parsed.max is not None:
        return (
            CompRange(min=parsed.min, max=parsed.max, currency=parsed.currency, source="parsed"),
            parsed.flags,
        )

    return CompRange(source="none"), []


class LeverSourcer:
    source = "lever"

    def __init__(self, client: PoliteClient):
        self.client = client

    def fetch(self, org: CompanyEntry, needs_detail: DetailPredicate | None = None) -> list[Role]:
        payload = self.client.get_json(POSTINGS_URL.format(slug=org.slug))
        roles: list[Role] = []

        for job in payload or []:
            comp, comp_flags = extract_comp(job)
            role = Role(
                id=Role.make_id(self.source, org.slug, job.get("id", "")),
                source=self.source,
                org_slug=org.slug,
                company_name=org.display_name,
                title=(job.get("text") or "").strip(),
                location_raw=_locations(job),
                is_remote=(job.get("workplaceType") or "").lower() == "remote" or None,
                comp=comp,
                url=job.get("hostedUrl") or job.get("applyUrl") or "",
                published_at=_parse_ms(job.get("createdAt")),
                description_text=job.get("descriptionPlain"),
            )
            for flag in comp_flags:
                role.flag(flag)
            roles.append(role)

        return roles
