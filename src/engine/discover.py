"""Vendor detection, slug extraction, and the per-vendor liveness probes.

Input: a company domain or a careers URL. Output: zero or more OrgRecord
candidates, built only from board URLs that literally appear on the company's
own page (links, iframes, embed scripts, canonical tags) or from where the
page redirects to. Nothing in here guesses a slug. A wrong slug is worse than
a missing one, because a wrong slug fills the registry with somebody else's
jobs.

The probes are the other half of the doctor: given (vendor, slug), ask that
vendor's public API whether the board answers and how many postings it holds.
check-org and registry verify both run on these, so the slug knowledge lives
in exactly one place.

Endpoint shapes verified live on 2026-07-25:
- ashby, greenhouse, lever, workable, workday answer 404 for an unknown slug.
- lever answers 200 with an empty list for a valid org with nothing published.
- workable's public account API is GET apply.workable.com/api/v1/widget/accounts/{slug}.
- smartrecruiters answers 200 with totalFound 0 for unknown AND for empty
  boards; the two cannot be told apart, so zero postings is recorded as dead
  with a note, and the self-healing registry flips it back if postings appear.
- workday's job search is POST {host}/wday/cxs/{tenant}/{site}/jobs.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import unquote

from pydantic import BaseModel, Field

from engine.registry import OrgRecord, Registry
from engine.sourcers.ashby import BOARD_URL as ASHBY_API
from engine.sourcers.base import OrgNotFound, OrgUnavailable, PoliteClient
from engine.sourcers.greenhouse import LIST_URL as GREENHOUSE_API
from engine.sourcers.lever import POSTINGS_URL as LEVER_API
from engine.sourcers.workday import split_slug

WORKABLE_API = "https://apply.workable.com/api/v1/widget/accounts/{slug}"
SMARTRECRUITERS_API = "https://api.smartrecruiters.com/v1/companies/{slug}/postings?limit=1"
WORKDAY_API = "https://{tenant}.wd{n}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs"

BOARD_URLS = {
    "ashby": "https://jobs.ashbyhq.com/{slug}",
    "greenhouse": "https://boards.greenhouse.io/{slug}",
    "lever": "https://jobs.lever.co/{slug}",
    "workable": "https://apply.workable.com/{slug}",
    "smartrecruiters": "https://jobs.smartrecruiters.com/{slug}",
}

# One slug character class for everyone. Ashby allows dots and percent-encoded
# spaces; the rest are plain. The class deliberately excludes the slash so a
# capture stops at the slug and never swallows a job path.
_SLUG = r"([A-Za-z0-9][A-Za-z0-9_.%-]*)"

# The greenhouse embed script addresses the board as ?for=slug, which the
# plain path pattern below would misread as the literal slug "embed".
_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("greenhouse", re.compile(r"greenhouse\.io/embed/job_board(?:/js)?\?[^\"'\s<>]*?for=" + _SLUG)),
    ("greenhouse", re.compile(r"(?:job-boards|boards)\.(?:eu\.)?greenhouse\.io/" + _SLUG)),
    ("greenhouse", re.compile(r"boards-api\.greenhouse\.io/v1/boards/" + _SLUG)),
    ("lever", re.compile(r"jobs\.(?:eu\.)?lever\.co/" + _SLUG)),
    ("ashby", re.compile(r"jobs\.ashbyhq\.com/" + _SLUG)),
    ("ashby", re.compile(r"api\.ashbyhq\.com/posting-api/job-board/" + _SLUG)),
    ("workable", re.compile(r"apply\.workable\.com/(?:api/v\d+/accounts/)?" + _SLUG)),
    ("smartrecruiters", re.compile(r"jobs\.smartrecruiters\.com/(?:sr-jobs/)?" + _SLUG)),
    ("smartrecruiters", re.compile(r"api\.smartrecruiters\.com/v1/companies/" + _SLUG)),
]

_WORKDAY = re.compile(
    r"([a-z0-9-]+)\.wd(\d{1,2})\.myworkdayjobs\.com/(?:([a-z]{2,3}(?:-[A-Za-z]{2,4})?)/)?" + _SLUG
)

# Path fragments the patterns can catch that are never org slugs.
_JUNK = {
    "greenhouse": {"embed", "js", "v1", "boards", "generic", "error", "img", "static"},
    "lever": {"login", "postings", "img"},
    "ashby": {"api", "posting-api", "embed", "non-user-facing"},
    "workable": {"api", "j", "jobs", "en", "backend", "assets"},
    "smartrecruiters": {"api", "sr-jobs", "job", "jobs", "img", "static"},
}

# Vendors whose slugs are case-insensitive and conventionally lowercase.
_LOWERCASE_VENDORS = {"greenhouse", "lever", "workable"}


class Probe(BaseModel):
    """What one vendor API said about one slug."""

    status: str  # "ok", "404", or "error"
    count: int = 0
    detail: str = ""
    company_name: str = ""
    sample: list[str] = Field(default_factory=list)


def _domain_of(target: str) -> str:
    stripped = re.sub(r"^https?://", "", target.strip().rstrip("/"))
    host = stripped.split("/")[0].lower()
    return host.removeprefix("www.")


def _name_guess(domain: str) -> str:
    """gong.io becomes Gong. A guess, and mark_live replaces it with the
    vendor's own answer whenever one exists."""
    label = domain.split(".")[0]
    return label.replace("-", " ").title()


def candidate_urls(target: str) -> list[str]:
    """A careers URL is fetched as given. A bare domain gets the conventional
    careers paths, cheapest guess first."""
    if target.startswith(("http://", "https://")):
        return [target]
    domain = _domain_of(target)
    return [
        f"https://{domain}/careers",
        f"https://{domain}/jobs",
        f"https://{domain}/",
    ]


def extract_board_refs(text: str) -> list[tuple[str, str, str]]:
    """Every (vendor, slug, board_url) the text mentions, deduped, order kept."""
    found: dict[tuple[str, str], str] = {}

    for vendor, pattern in _PATTERNS:
        for match in pattern.finditer(text):
            slug = unquote(match.group(1)).strip().rstrip(".-")
            if vendor in _LOWERCASE_VENDORS:
                slug = slug.lower()
            if not slug or slug.lower() in _JUNK.get(vendor, set()):
                continue
            found.setdefault((vendor, slug), BOARD_URLS[vendor].format(slug=slug))

    for match in _WORKDAY.finditer(text):
        tenant, n, _locale, site = match.groups()
        if site and site.lower() != "wday":
            slug = f"{tenant}.wd{n}/{site}"
            url = f"https://{tenant}.wd{n}.myworkdayjobs.com/{site}"
            found.setdefault(("workday", slug), url)

    return [(vendor, slug, url) for (vendor, slug), url in found.items()]


def discover(target: str, client: PoliteClient, discovered_via: str = "") -> list[OrgRecord]:
    """Fetch the careers page and read the board URLs off it.

    Returns [] when nothing matches. The caller records that as a vendor
    "unknown" row rather than discarding it, so the domain is not re-scanned
    on every pass.
    """
    domain = _domain_of(target)
    for url in candidate_urls(target):
        try:
            resp = client.get(url)
        except OrgUnavailable:
            continue
        if resp.status_code >= 400:
            continue

        # The final URL after redirects is evidence too: plenty of careers
        # pages are nothing but a redirect straight to the board.
        haystack = f"{resp.url}\n{resp.text}"
        refs = extract_board_refs(haystack)
        if refs:
            return [
                OrgRecord(
                    company_name=_name_guess(domain),
                    domain=domain,
                    vendor=vendor,
                    slug=slug,
                    board_url=board_url,
                    status="unverified",
                    discovered_via=discovered_via,
                )
                for vendor, slug, board_url in refs
            ]
    return []


def no_match_record(target: str, discovered_via: str = "") -> OrgRecord:
    """The resumability marker for a domain that showed no known board."""
    domain = _domain_of(target)
    return OrgRecord(
        company_name=_name_guess(domain),
        domain=domain,
        vendor="unknown",
        slug=domain,
        status="unverified",
        discovered_via=discovered_via,
        notes="no known ATS pattern on the careers page",
    )


# Probes ------------------------------------------------------------------


def _probe_ashby(slug: str, client: PoliteClient) -> Probe:
    payload = client.get_json(ASHBY_API.format(slug=slug))
    jobs = [j for j in payload.get("jobs") or [] if j.get("isListed", True)]
    return Probe(
        status="ok",
        count=len(jobs),
        detail=f"{len(jobs)} listed jobs",
        sample=[j.get("title", "") for j in jobs[:3]],
    )


def _probe_greenhouse(slug: str, client: PoliteClient) -> Probe:
    payload = client.get_json(GREENHOUSE_API.format(slug=slug))
    jobs = payload.get("jobs") or []
    name = next((j.get("company_name") for j in jobs if j.get("company_name")), "")
    return Probe(
        status="ok",
        count=len(jobs),
        detail=f"{len(jobs)} listed jobs",
        company_name=name or "",
        sample=[j.get("title", "") for j in jobs[:3]],
    )


def _probe_lever(slug: str, client: PoliteClient) -> Probe:
    payload = client.get_json(LEVER_API.format(slug=slug))
    jobs = payload if isinstance(payload, list) else []
    return Probe(
        status="ok",
        count=len(jobs),
        detail=f"{len(jobs)} published postings",
        sample=[j.get("text", "") for j in jobs[:3]],
    )


def _probe_workable(slug: str, client: PoliteClient) -> Probe:
    payload = client.get_json(WORKABLE_API.format(slug=slug))
    jobs = payload.get("jobs") or []
    return Probe(
        status="ok",
        count=len(jobs),
        detail=f"{len(jobs)} published jobs",
        company_name=payload.get("name") or "",
        sample=[j.get("title", "") for j in jobs[:3]],
    )


def _probe_smartrecruiters(slug: str, client: PoliteClient) -> Probe:
    payload = client.get_json(SMARTRECRUITERS_API.format(slug=slug))
    total = int(payload.get("totalFound") or 0)
    if total == 0:
        # The API answers this for unknown companies and for empty boards
        # alike. Verified 2026-07-25. Dead-with-a-note is the honest verdict,
        # and the registry flips it back the day postings appear.
        raise OrgNotFound(f"smartrecruiters reports 0 postings for {slug}")
    sample = [p.get("name", "") for p in payload.get("content") or []]
    return Probe(status="ok", count=total, detail=f"{total} published postings", sample=sample)


def _probe_workday(slug: str, client: PoliteClient) -> Probe:
    tenant, wd, site = split_slug(slug)
    payload = client.post_json(
        WORKDAY_API.format(tenant=tenant, n=wd, site=site),
        json={"appliedFacets": {}, "limit": 3, "offset": 0, "searchText": ""},
    )
    total = int(payload.get("total") or 0)
    return Probe(
        status="ok",
        count=total,
        detail=f"{total} published postings",
        sample=[p.get("title", "") for p in payload.get("jobPostings") or []],
    )


_PROBES = {
    "ashby": _probe_ashby,
    "greenhouse": _probe_greenhouse,
    "lever": _probe_lever,
    "workable": _probe_workable,
    "smartrecruiters": _probe_smartrecruiters,
    "workday": _probe_workday,
}

PROBE_VENDORS: tuple[str, ...] = tuple(_PROBES)


def probe_org(vendor: str, slug: str, client: PoliteClient) -> Probe:
    """Ask one vendor's public API about one slug. Never raises."""
    probe = _PROBES.get(vendor)
    if probe is None:
        return Probe(status="error", detail=f"no probe for vendor {vendor}")
    try:
        return probe(slug, client)
    except OrgNotFound as exc:
        return Probe(status="404", detail=str(exc) or "no board at this slug")
    except OrgUnavailable as exc:
        return Probe(status="error", detail=str(exc))


# Bulk passes --------------------------------------------------------------
#
# Both passes run at most `workers` requests in flight, mutate the registry
# only on the main thread, and save as they go, so an interrupted 3am run
# loses at most one save interval of work and simply resumes.

MAX_WORKERS = 4
Progress = Callable[[str], None]


def _noop(_msg: str) -> None:
    return None


def verify_registry(
    registry: Registry,
    client: PoliteClient,
    vendor: str | None = None,
    limit: int | None = None,
    workers: int = MAX_WORKERS,
    today: date | None = None,
    progress: Progress = _noop,
    save_every: int = 50,
) -> dict[str, int]:
    """Re-check records against their vendor's public API, stalest first."""
    when = today or date.today()
    records = [
        r
        for r in registry.records.values()
        if r.vendor != "unknown" and (vendor is None or r.vendor == vendor)
    ]
    # Never-verified rows first, then the ones whose facts are oldest.
    records.sort(key=lambda r: (r.last_verified is not None, r.last_verified or when, r.key))
    if limit is not None:
        records = records[:limit]

    counts = {"checked": 0, "live": 0, "dead": 0, "error": 0, "went_dead": 0, "revived": 0}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(probe_org, r.vendor, r.slug, client): r for r in records}
        for future in as_completed(futures):
            record = futures[future]
            try:
                probe = future.result()
            except Exception as exc:  # noqa: BLE001 - a 3am pass must outlive one bad org
                probe = Probe(status="error", detail=f"{type(exc).__name__}: {exc}")
            counts["checked"] += 1

            if probe.status == "ok":
                if record.status == "dead":
                    counts["revived"] += 1
                    progress(f"revived: {record.vendor}/{record.slug} ({probe.count} postings)")
                registry.mark_live(
                    record.vendor, record.slug, probe.count, when, probe.company_name
                )
                counts["live"] += 1
            elif probe.status == "404":
                if record.status == "live":
                    counts["went_dead"] += 1
                    progress(f"went dead: {record.vendor}/{record.slug}: {probe.detail}")
                registry.mark_dead(record.vendor, record.slug, when, probe.detail)
                counts["dead"] += 1
            else:
                # A timeout is not a verdict. Leave the record as it was and
                # let the next pass try again.
                counts["error"] += 1

            if counts["checked"] % save_every == 0:
                registry.save()
                progress(f"{counts['checked']}/{len(records)} checked")

    registry.save()
    return counts


def add_domains(
    registry: Registry,
    domains: Iterable[str],
    client: PoliteClient,
    discovered_via: str,
    workers: int = MAX_WORKERS,
    progress: Progress = _noop,
    save_every: int = 25,
) -> dict[str, int]:
    """Discover boards for a list of domains and fold them into the registry.

    Domains the registry has already seen are skipped, which is what makes a
    multi-thousand-domain pass resumable after an interrupt.
    """
    seen = registry.domains_seen()
    todo: list[str] = []
    counts = {"scanned": 0, "skipped": 0, "orgs_found": 0, "no_match": 0}
    for raw in domains:
        domain = _domain_of(raw)
        if not domain or domain.startswith("#"):
            continue
        if domain in seen:
            counts["skipped"] += 1
            continue
        seen.add(domain)
        todo.append(domain)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(discover, domain, client, discovered_via): domain for domain in todo
        }
        for future in as_completed(futures):
            domain = futures[future]
            counts["scanned"] += 1
            try:
                records = future.result()
            except Exception as exc:  # noqa: BLE001 - one weird page must not end the pass
                # No marker row either: an error is not "no board here", and
                # the domain deserves a retry on the next pass.
                progress(f"error: {domain}: {type(exc).__name__}: {exc}")
                continue

            if records:
                counts["orgs_found"] += len(records)
                for record in records:
                    registry.upsert(record)
                    progress(f"found: {domain} uses {record.vendor} as {record.slug}")
            else:
                counts["no_match"] += 1
                registry.upsert(no_match_record(domain, discovered_via))

            if counts["scanned"] % save_every == 0:
                registry.save()
                progress(f"{counts['scanned']}/{len(todo)} domains scanned")

    registry.save()
    return counts
