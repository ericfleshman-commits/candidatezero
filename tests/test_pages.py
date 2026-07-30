"""Per-company answer pages and the machine-readable files beside them.

The pages inherit the board's firewall by construction: a page renders from
the org's registry row and the public listing store, and from nothing else.
These tests pin the public artifacts themselves: the OG cards a link unfurls
into, llms.txt for AI assistants, and sitemap.xml for crawlers.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from conftest import make_role
from engine import pages
from engine.board import BASE_URL, OG_IMAGE
from engine.public_store import PublicStore
from engine.registry import OrgRecord

NOW = datetime(2026, 7, 26, 3, 0, tzinfo=UTC)
RUN_NIGHT = datetime(2026, 7, 21, 3, 0, tzinfo=UTC)

SCANNED = {"ashby:acme", "greenhouse:quietco"}


def _orgs() -> list[OrgRecord]:
    return [
        OrgRecord(
            vendor="ashby",
            slug="acme",
            company_name="Acme",
            status="live",
            last_verified=date(2026, 7, 21),
        ),
        OrgRecord(
            vendor="greenhouse",
            slug="quietco",
            company_name="QuietCo",
            status="live",
            last_verified=date(2026, 7, 20),
        ),
    ]


@pytest.fixture
def store() -> PublicStore:
    s = PublicStore()
    s.reconcile(
        [
            make_role(id="ashby:acme:1", title="GTM Engineer"),
            make_role(id="ashby:acme:2", title="RevOps Engineer"),
        ],
        SCANNED,
        now=RUN_NIGHT,
    )
    return s


@pytest.fixture
def report(store) -> pages.PagesReport:
    return pages.build_report(_orgs(), store, now=NOW)


def _page(report: pages.PagesReport, dirname: str) -> pages.CompanyPage:
    return next(p for p in report.pages if p.dirname == dirname)


def test_hiring_page_og_tags_answer_the_question(report):
    body = pages.render_page(report, _page(report, "ashby-acme"))

    assert (
        '<meta property="og:title" content="Is Acme hiring GTM engineers'
        ' right now? | CandidateZero">' in body
    )
    assert (
        '<meta property="og:description" content="Yes: 2 open GTM/RevOps roles'
        ' as of 2026-07-21.">' in body
    )
    assert f'<meta property="og:url" content="{BASE_URL}/companies/ashby-acme/">' in body
    assert '<meta property="og:type" content="website">' in body
    assert f'<meta property="og:image" content="{OG_IMAGE}">' in body
    assert '<meta name="twitter:card" content="summary_large_image">' in body


def test_quiet_page_og_description_answers_no(report):
    body = pages.render_page(report, _page(report, "greenhouse-quietco"))

    assert (
        '<meta property="og:description" content="No live GTM or RevOps roles'
        ' verified as of 2026-07-26.">' in body
    )
    assert f'<meta property="og:url" content="{BASE_URL}/companies/greenhouse-quietco/">' in body


def test_every_company_page_footer_links_the_methodology(report):
    for page in report.pages:
        body = pages.render_page(report, page)
        assert 'href="../../methodology/index.html"' in body


def test_sitemap_lists_every_public_page_with_lastmod(report):
    xml = pages.sitemap_xml(report)

    assert xml.startswith('<?xml version="1.0" encoding="UTF-8"?>')
    assert '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">' in xml
    assert f"<loc>{BASE_URL}/</loc>" in xml
    assert f"<loc>{BASE_URL}/companies/</loc>" in xml
    assert f"<loc>{BASE_URL}/methodology/</loc>" in xml
    # Each company page carries the verification date its headline stands on.
    assert (
        f"<url><loc>{BASE_URL}/companies/ashby-acme/</loc>"
        "<lastmod>2026-07-21</lastmod></url>" in xml
    )
    assert (
        f"<url><loc>{BASE_URL}/companies/greenhouse-quietco/</loc>"
        "<lastmod>2026-07-20</lastmod></url>" in xml
    )
    assert xml.rstrip().endswith("</urlset>")
    assert xml.count("<url>") == 3 + len(report.pages)


def test_llms_txt_names_the_methodology_and_the_quotable_stat(report):
    text = pages.llms_txt(report)

    assert "(methodology/index.html)" in text
    for vendor in ("Ashby", "Greenhouse", "Lever", "Workable", "SmartRecruiters", "Workday"):
        assert vendor in text
    assert "Quotable stat, one line" in text
    assert "2 company\nboards watched, 1 hiring now, 2 live" in text
    assert "sitemap.xml" in text


def test_write_lands_the_whole_public_tree(tmp_path, report):
    pages.write(report, tmp_path)

    board_dir = tmp_path / "board"
    assert (board_dir / "companies" / "index.html").is_file()
    assert (board_dir / "companies" / "ashby-acme" / "index.html").is_file()
    assert (board_dir / "companies" / "greenhouse-quietco" / "index.html").is_file()
    assert (board_dir / "llms.txt").is_file()
    assert (board_dir / "sitemap.xml").is_file()
