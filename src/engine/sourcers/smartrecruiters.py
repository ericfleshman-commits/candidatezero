"""SmartRecruiters public postings API.

List:   GET https://api.smartrecruiters.com/v1/companies/{slug}/postings
Detail: GET https://api.smartrecruiters.com/v1/companies/{slug}/postings/{id}

Shape verified live on 2026-07-26 against the HireVue board. The list is
paginated (limit up to 100, offset), answers {totalFound, content: [...]},
and carries name, releasedDate, and a location object with an explicit
remote boolean, but no description and no comp. The number, if it exists,
is prose inside the detail response's jobAd sections, so the detail fetch
is gated on needs_detail exactly like Greenhouse.

One honest wart, shared with the registry probe: the API answers 200 with
totalFound 0 for unknown slugs and empty boards alike. An empty list here is
a quiet board; the registry is what decides a slug is dead.

The list has no posting URL either. It is constructed from the company
identifier and posting id, which the site answers, and the detail response's
own postingUrl replaces it whenever detail is fetched.
"""

from __future__ import annotations

from datetime import datetime

from engine.comp import html_to_text, parse_salary_text
from engine.config import CompanyEntry
from engine.models import CompRange, Role
from engine.sourcers.base import DetailPredicate, OrgNotFound, OrgUnavailable, PoliteClient

LIST_URL = (
    "https://api.smartrecruiters.com/v1/companies/{slug}/postings?limit={limit}&offset={offset}"
)
DETAIL_URL = "https://api.smartrecruiters.com/v1/companies/{slug}/postings/{posting_id}"
POSTING_URL = "https://jobs.smartrecruiters.com/{company}/{posting_id}"

PAGE_SIZE = 100

# jobAd sections in reading order. companyDescription is included on purpose:
# for a staffing firm that is exactly where the "our client" tell lives.
_SECTIONS = ("companyDescription", "jobDescription", "qualifications", "additionalInformation")


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _location(posting: dict) -> str:
    loc = posting.get("location") or {}
    full = (loc.get("fullLocation") or "").strip()
    if full:
        return full
    return ", ".join(
        p.strip() for p in (loc.get("city"), loc.get("region"), loc.get("country")) if p
    )


class SmartRecruitersSourcer:
    source = "smartrecruiters"

    def __init__(self, client: PoliteClient):
        self.client = client
        self.detail_fetches = 0

    def fetch(self, org: CompanyEntry, needs_detail: DetailPredicate | None = None) -> list[Role]:
        postings = self._pages(org)
        roles: list[Role] = []

        for posting in postings:
            company = posting.get("company") or {}
            loc = posting.get("location") or {}
            posting_id = posting.get("id", "")
            role = Role(
                id=Role.make_id(self.source, org.slug, posting_id),
                source=self.source,
                org_slug=org.slug,
                company_name=company.get("name") or org.display_name,
                title=(posting.get("name") or "").strip(),
                location_raw=_location(posting),
                is_remote=True if loc.get("remote") else None,
                comp=CompRange(source="none"),
                url=POSTING_URL.format(
                    company=company.get("identifier") or org.slug, posting_id=posting_id
                ),
                published_at=_parse_dt(posting.get("releasedDate")),
            )

            if needs_detail is None or needs_detail(role):
                self._enrich(org, posting_id, role)

            roles.append(role)

        return roles

    def _pages(self, org: CompanyEntry) -> list[dict]:
        """Walk the pagination until totalFound is in hand or a page comes back empty."""
        postings: list[dict] = []
        total = None
        while total is None or len(postings) < total:
            payload = self.client.get_json(
                LIST_URL.format(slug=org.slug, limit=PAGE_SIZE, offset=len(postings))
            )
            total = int(payload.get("totalFound") or 0)
            page = payload.get("content") or []
            if not page:
                break
            postings.extend(page)
        return postings

    def _enrich(self, org: CompanyEntry, posting_id: str, role: Role) -> None:
        """Fetch the jobAd and try to read a band out of its prose.

        Same contract as Greenhouse: a failed detail fetch flags the role and
        never takes down the org, let alone the run.
        """
        try:
            detail = self.client.get_json(DETAIL_URL.format(slug=org.slug, posting_id=posting_id))
        except (OrgNotFound, OrgUnavailable):
            role.flag("detail-fetch-failed")
            return

        self.detail_fetches += 1
        if detail.get("postingUrl"):
            role.url = detail["postingUrl"]

        sections = (detail.get("jobAd") or {}).get("sections") or {}
        parts: list[str] = []
        for name in _SECTIONS:
            section = sections.get(name) or {}
            text = html_to_text(section.get("text"))
            if text:
                parts.append(text)
        role.description_text = "\n".join(parts) or None

        parsed = parse_salary_text(role.description_text)
        if parsed.min is not None or parsed.max is not None:
            role.comp = CompRange(
                min=parsed.min, max=parsed.max, currency=parsed.currency, source="parsed"
            )
            for flag in parsed.flags:
                role.flag(flag)
