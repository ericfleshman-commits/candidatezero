"""Greenhouse job board API.

List:   GET https://boards-api.greenhouse.io/v1/boards/{slug}/jobs
Detail: GET https://boards-api.greenhouse.io/v1/boards/{slug}/jobs/{id}

The list response has no pay in it at all. The number, if it exists, is prose
inside the detail response's description. Gong's board carries 102 postings; a
detail fetch for every one of them would be 102 extra requests to learn nothing
about 100 roles we were never going to keep.

So the detail fetch is gated on needs_detail, which the pipeline wires to the
title and location filters. Cheap checks first, expensive checks last, and we
stay a polite guest.
"""

from __future__ import annotations

from datetime import datetime

from engine.comp import html_to_text, parse_salary_text
from engine.config import CompanyEntry
from engine.models import CompRange, Role
from engine.sourcers.base import DetailPredicate, OrgNotFound, OrgUnavailable, PoliteClient

LIST_URL = "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"
DETAIL_URL = "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs/{job_id}"


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


class GreenhouseSourcer:
    source = "greenhouse"

    def __init__(self, client: PoliteClient):
        self.client = client
        self.detail_fetches = 0

    def fetch(self, org: CompanyEntry, needs_detail: DetailPredicate | None = None) -> list[Role]:
        payload = self.client.get_json(LIST_URL.format(slug=org.slug))
        roles: list[Role] = []

        for job in payload.get("jobs") or []:
            location = ((job.get("location") or {}).get("name") or "").strip()
            role = Role(
                id=Role.make_id(self.source, org.slug, job.get("id", "")),
                source=self.source,
                org_slug=org.slug,
                # Greenhouse tells us the legal entity name, which is often better
                # than what we guessed in companies.yaml.
                company_name=job.get("company_name") or org.display_name,
                title=(job.get("title") or "").strip(),
                location_raw=location,
                url=job.get("absolute_url") or "",
                published_at=_parse_dt(job.get("first_published")),
                updated_at=_parse_dt(job.get("updated_at")),
                comp=CompRange(source="none"),
            )

            if needs_detail is None or needs_detail(role):
                self._enrich(org, job, role)

            roles.append(role)

        return roles

    def _enrich(self, org: CompanyEntry, job: dict, role: Role) -> None:
        """Fetch the posting body and try to read a band out of it.

        A detail fetch that fails must not take down the org, let alone the run.
        The role simply keeps comp_source "none" and shows up flagged as unknown.
        """
        url = DETAIL_URL.format(slug=org.slug, job_id=job.get("id"))
        try:
            detail = self.client.get_json(url)
        except (OrgNotFound, OrgUnavailable):
            role.flag("detail-fetch-failed")
            return

        self.detail_fetches += 1
        role.description_text = html_to_text(detail.get("content"))

        parsed = parse_salary_text(role.description_text)
        if parsed.min is not None or parsed.max is not None:
            role.comp = CompRange(
                min=parsed.min,
                max=parsed.max,
                currency=parsed.currency,
                source="parsed",
            )
            for flag in parsed.flags:
                role.flag(flag)
