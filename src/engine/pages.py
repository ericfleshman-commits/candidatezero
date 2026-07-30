"""Per-company answer pages: data/board/companies/{vendor}-{slug}/index.html.

One page per live org in the registry, each answering the one question a
searcher actually types: is this company hiring GTM engineers right now? The
answer is a yes with the verified roles, or an honest no with the date the
company's own board was last read. Nobody else can publish these pages
truthfully, because nobody else re-verifies nightly.

The board's firewall is inherited by construction. A page renders from the
org's registry row (vendor, slug, company name, verification date) and the
public listing store, and from nothing else: private flags, suppression
history and the operator's own rules have no path in. test_pages.py holds the
publish gate that proves it, page by page.
"""

from __future__ import annotations

import json
import re
import shutil
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, Field

from engine.board import BASE_URL, html_env, location_label, write_stylesheet
from engine.public_store import PublicRole, PublicStore
from engine.registry import OrgRecord


class PageRole(BaseModel):
    """One listed role, the same public fields the board prints."""

    title: str
    comp_band: str = ""  # empty renders as "not published"
    location: str
    posted: str
    url: str = ""


class CompanyPage(BaseModel):
    """One company's answer. Everything here is registry or store data."""

    company: str
    dirname: str
    hiring: bool = False
    roles: list[PageRole] = Field(default_factory=list)
    as_of: str  # the verification date the headline stands on
    last_checked: str  # full timestamp when the store has one, date otherwise
    jsonld: str = ""  # pre-serialized and script-safe, see _jsonld


class PagesReport(BaseModel):
    """Everything the company pages and their index render."""

    generated: str
    generated_day: str
    pages: list[CompanyPage] = Field(default_factory=list)
    hiring_count: int = 0
    quiet_count: int = 0
    total_roles: int = 0


def _dirname(vendor: str, slug: str) -> str:
    """A slug is not a path: Workday slugs carry dots and slashes, others carry
    spaces and capitals. Fold everything to one safe hyphenated segment."""
    return re.sub(r"[^a-z0-9]+", "-", f"{vendor}-{slug}".lower()).strip("-")


def _day(value: datetime | None) -> str:
    return value.date().isoformat() if value else "not stated"


def _page_role(entry: PublicRole) -> PageRole:
    return PageRole(
        title=entry.title,
        comp_band=entry.comp_band,
        location=location_label(entry),
        posted=_day(entry.posted_at),
        url=entry.url,
    )


def _jsonld(entries: list[PublicRole]) -> str:
    """schema.org JobPosting per live role, employer-published data only.

    The comp band is deliberately absent: baseSalary wants structured numbers
    and the store holds the employer's human-readable band, so putting numbers
    here would mean re-deriving them. When in doubt, exclude.

    Serialized here rather than in the template because the template
    autoescapes; "</" is escaped so third-party text can never close the
    script tag early.
    """
    postings = []
    for entry in entries:
        posting: dict = {
            "@context": "https://schema.org/",
            "@type": "JobPosting",
            "title": entry.title,
            "hiringOrganization": {"@type": "Organization", "name": entry.company},
            "url": entry.url,
            "directApply": True,
        }
        if entry.posted_at is not None:
            posting["datePosted"] = entry.posted_at.date().isoformat()
        if entry.location:
            posting["jobLocation"] = {
                "@type": "Place",
                "address": {"@type": "PostalAddress", "addressLocality": entry.location},
            }
        if entry.remote:
            posting["jobLocationType"] = "TELECOMMUTE"
        postings.append(posting)
    return json.dumps(postings, ensure_ascii=False, indent=1).replace("</", "<\\/")


def build_report(orgs: list[OrgRecord], store: PublicStore, now: datetime) -> PagesReport:
    # Live sightings per org board. The main board collapses cross-vendor
    # mirrors to kill duplicates, but a mirror is still a live posting on THIS
    # org's board; a company page that hid it would answer no about a company
    # that is hiring.
    by_org: dict[str, list[PublicRole]] = {}
    verified: dict[str, datetime] = {}
    for entry in store.roles.values():
        if entry.closed_at is None:
            by_org.setdefault(entry.org_key, []).append(entry)
        stamp = verified.get(entry.org_key)
        if stamp is None or entry.last_verified > stamp:
            verified[entry.org_key] = entry.last_verified

    report = PagesReport(
        generated=now.strftime("%Y-%m-%d %H:%M UTC"),
        generated_day=now.date().isoformat(),
    )

    taken: set[str] = set()
    for org in orgs:
        entries = sorted(
            by_org.get(f"{org.vendor}:{org.slug}", []), key=lambda e: (e.title.lower(), e.id)
        )
        dirname = _dirname(org.vendor, org.slug)
        while dirname in taken:  # two slugs can sanitize to the same segment
            dirname += "-2"
        taken.add(dirname)

        # The registry verify pass and the store's sightings both count as
        # reading the company's board; the page stands on the most recent one.
        stamp = verified.get(f"{org.vendor}:{org.slug}")
        if stamp is not None and (org.last_verified is None or stamp.date() >= org.last_verified):
            as_of = stamp.date().isoformat()
            last_checked = stamp.strftime("%Y-%m-%d %H:%M UTC")
        elif org.last_verified is not None:
            as_of = org.last_verified.isoformat()
            last_checked = org.last_verified.isoformat()
        else:  # a live row always carries a date; belt and braces
            as_of = report.generated_day
            last_checked = report.generated_day

        report.pages.append(
            CompanyPage(
                company=org.company_name or (entries[0].company if entries else org.slug),
                dirname=dirname,
                hiring=bool(entries),
                roles=[_page_role(e) for e in entries],
                as_of=as_of,
                last_checked=last_checked,
                jsonld=_jsonld(entries) if entries else "",
            )
        )
        report.total_roles += len(entries)

    report.hiring_count = sum(1 for p in report.pages if p.hiring)
    report.quiet_count = len(report.pages) - report.hiring_count
    # Hiring companies first, then alphabetical: the index reads as a ranking.
    report.pages.sort(key=lambda p: (-len(p.roles), p.company.lower(), p.dirname))
    return report


def render_page(report: PagesReport, page: CompanyPage, template_dir: Path | None = None) -> str:
    return html_env(template_dir).get_template("company.html.j2").render(report=report, page=page)


def render_index(report: PagesReport, template_dir: Path | None = None) -> str:
    return html_env(template_dir).get_template("companies.html.j2").render(report=report)


def sitemap_xml(report: PagesReport) -> str:
    """Standard sitemap at data/board/sitemap.xml, one url per public page.

    A company page's lastmod is its as_of date, the same registry or store
    verification the page's own headline stands on. The three top pages are
    regenerated every run and carry the run date. Every loc is built from
    BASE_URL and a sanitized dirname, so nothing here needs XML escaping.
    """
    entries = [
        (f"{BASE_URL}/", report.generated_day),
        (f"{BASE_URL}/companies/", report.generated_day),
        (f"{BASE_URL}/methodology/", report.generated_day),
    ]
    entries += [(f"{BASE_URL}/companies/{p.dirname}/", p.as_of) for p in report.pages]

    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
    ]
    lines += [f" <url><loc>{loc}</loc><lastmod>{lastmod}</lastmod></url>" for loc, lastmod in entries]
    lines.append("</urlset>")
    return "\n".join(lines) + "\n"


def llms_txt(report: PagesReport) -> str:
    """The AI-assistant entry point, at data/board/llms.txt."""
    return f"""# CandidateZero

> A verified board of GTM engineering, RevOps and revenue systems roles.
> Listings come straight from each employer's own applicant tracking system
> and are re-verified every night; a role that disappears is closed the same
> night, so nothing here is stale by more than a day.

Methodology, quotable in one line: every listing on CandidateZero is
re-verified on the employer's own applicant tracking system during the most
recent nightly run, and the engine never applies for anyone.

## Pages

- [The verified board](index.html): every role verified live right now, what
  is new this week, and what closed recently.
- [Company index](companies/index.html): every company watched, with its live
  GTM/RevOps role count and last-verified date, hiring companies first.
- Per-company pages at companies/{{vendor}}-{{slug}}/index.html: each answers
  "is this company hiring GTM engineers right now?" from verified data, with
  schema.org JobPosting markup for every live role.
- [Methodology](methodology/index.html): what "verified live" means, which six
  hiring systems are checked (Ashby, Greenhouse, Lever, Workable,
  SmartRecruiters, Workday), and how titles are matched, in plain words.

Quotable stat, one line, as of {report.generated}: {len(report.pages)} company
boards watched, {report.hiring_count} hiring now, {report.total_roles} live
GTM/RevOps roles verified on the employers' own systems.

Apply links go straight to the employer, never through an aggregator.
A full URL list with last-verified dates is in sitemap.xml.
"""


def write(report: PagesReport, data_dir: Path, template_dir: Path | None = None) -> Path:
    """Write every page, the index, llms.txt, sitemap.xml and the stylesheet.

    The companies tree is rebuilt from scratch each run: these are generated
    files, and a page for an org that has since gone dead must not linger
    answering with a stale date.
    """
    board_dir = data_dir / "board"
    companies = board_dir / "companies"
    if companies.exists():
        shutil.rmtree(companies)
    companies.mkdir(parents=True)

    env = html_env(template_dir)
    page_tpl = env.get_template("company.html.j2")
    for page in report.pages:
        target = companies / page.dirname
        target.mkdir()
        (target / "index.html").write_text(
            page_tpl.render(report=report, page=page), encoding="utf-8"
        )

    (companies / "index.html").write_text(
        env.get_template("companies.html.j2").render(report=report), encoding="utf-8"
    )
    (board_dir / "llms.txt").write_text(llms_txt(report), encoding="utf-8")
    (board_dir / "sitemap.xml").write_text(sitemap_xml(report), encoding="utf-8")
    write_stylesheet(board_dir)
    return companies
