"""Workday cxs job search.

List:   POST https://{tenant}.wd{n}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs
Detail: GET  https://{tenant}.wd{n}.myworkdayjobs.com/wday/cxs/{tenant}/{site}{externalPath}

Shape verified live on 2026-07-26 against the Workday tenant's own board. The
list is a plain public POST, no auth, no cookies, paginated at 20 per page,
answering {total, jobPostings: [{title, externalPath, locationsText,
postedOn, remoteType, bulletFields}]}. Two honest gaps in the list:

- postedOn is relative prose ("Posted Yesterday"), so published_at stays
  unknown until a detail fetch reads the ISO startDate.
- there is no description and no comp. Both live in the detail response's
  jobPostingInfo, so the detail fetch is gated on needs_detail like
  Greenhouse. US postings tend to carry a pay-transparency band in the
  jobDescription prose, parsed and recorded as an inference.

A registry slug for Workday is "{tenant}.wd{n}/{site}", because a bare slug
cannot address a Workday board at all. Same convention as the probe.
"""

from __future__ import annotations

from datetime import datetime

from engine.comp import html_to_text, parse_salary_text
from engine.config import CompanyEntry
from engine.models import CompRange, Role
from engine.sourcers.base import DetailPredicate, OrgNotFound, OrgUnavailable, PoliteClient

HOST_URL = "https://{tenant}.wd{n}.myworkdayjobs.com"
JOBS_URL = HOST_URL + "/wday/cxs/{tenant}/{site}/jobs"
DETAIL_URL = HOST_URL + "/wday/cxs/{tenant}/{site}{path}"

PAGE_SIZE = 20

_REMOTE_TYPES = {"remote": True, "onsite": False}


def split_slug(slug: str) -> tuple[str, str, str]:
    """ "workday.wd5/Workday" becomes (workday, 5, Workday). Malformed is a 404-class
    fact: the slug cannot address any board, tonight or ever."""
    if "/" not in slug or ".wd" not in slug:
        raise OrgNotFound(f"not a workday tenant/site slug: {slug}")
    host_part, site = slug.split("/", 1)
    tenant, n = host_part.split(".wd", 1)
    return tenant, n, site


def _parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


class WorkdaySourcer:
    source = "workday"

    def __init__(self, client: PoliteClient):
        self.client = client
        self.detail_fetches = 0

    def fetch(self, org: CompanyEntry, needs_detail: DetailPredicate | None = None) -> list[Role]:
        tenant, n, site = split_slug(org.slug)
        host = HOST_URL.format(tenant=tenant, n=n)
        postings = self._pages(tenant, n, site)
        roles: list[Role] = []

        for job in postings:
            path = job.get("externalPath") or ""
            remote_type = (job.get("remoteType") or "").strip().lower()
            role = Role(
                # externalPath is the only id the list guarantees; bulletFields
                # usually carries the req id but arrives empty on some tenants.
                id=Role.make_id(self.source, org.slug, path),
                source=self.source,
                org_slug=org.slug,
                company_name=org.display_name,
                title=(job.get("title") or "").strip(),
                location_raw=(job.get("locationsText") or "").strip(),
                is_remote=_REMOTE_TYPES.get(remote_type),
                comp=CompRange(source="none"),
                url=f"{host}/{site}{path}",
            )

            if path and (needs_detail is None or needs_detail(role)):
                self._enrich(tenant, n, site, path, role)

            roles.append(role)

        return roles

    def _pages(self, tenant: str, n: str, site: str) -> list[dict]:
        """Walk the pagination until total is in hand or a page comes back empty."""
        url = JOBS_URL.format(tenant=tenant, n=n, site=site)
        postings: list[dict] = []
        total = None
        while total is None or len(postings) < total:
            payload = self.client.post_json(
                url,
                json={
                    "appliedFacets": {},
                    "limit": PAGE_SIZE,
                    "offset": len(postings),
                    "searchText": "",
                },
            )
            total = int(payload.get("total") or 0)
            page = payload.get("jobPostings") or []
            if not page:
                break
            postings.extend(page)
        return postings

    def _enrich(self, tenant: str, n: str, site: str, path: str, role: Role) -> None:
        """Fetch jobPostingInfo: the JD text, the ISO posting date, the real URL.

        Same contract as Greenhouse: a failed detail fetch flags the role and
        never takes down the org, let alone the run.
        """
        try:
            detail = self.client.get_json(
                DETAIL_URL.format(tenant=tenant, n=n, site=site, path=path)
            )
        except (OrgNotFound, OrgUnavailable):
            role.flag("detail-fetch-failed")
            return

        self.detail_fetches += 1
        info = detail.get("jobPostingInfo") or {}
        if info.get("externalUrl"):
            role.url = info["externalUrl"]
        role.published_at = _parse_date(info.get("startDate"))
        role.description_text = html_to_text(info.get("jobDescription"))

        parsed = parse_salary_text(role.description_text)
        if parsed.min is not None or parsed.max is not None:
            role.comp = CompRange(
                min=parsed.min, max=parsed.max, currency=parsed.currency, source="parsed"
            )
            for flag in parsed.flags:
                role.flag(flag)
