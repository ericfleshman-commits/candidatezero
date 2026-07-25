"""Vendor detection, slug extraction, and the probes.

One detection test per vendor, because every extractor pattern is a promise
that a real careers page shape resolves to the right slug. The HTML snippets
mirror the shapes seen in the wild on 2026-07-25: plain links, iframes, the
greenhouse embed script, canonical tags, and a careers page that is nothing
but a redirect to the board.
"""

from __future__ import annotations

import httpx
import respx

from engine.discover import (
    SMARTRECRUITERS_API,
    WORKABLE_API,
    candidate_urls,
    discover,
    extract_board_refs,
    no_match_record,
    probe_org,
)
from engine.sourcers.lever import POSTINGS_URL

# Detection ----------------------------------------------------------------


def refs(html: str) -> dict[str, str]:
    return {vendor: slug for vendor, slug, _url in extract_board_refs(html)}


def test_greenhouse_plain_board_link():
    html = '<a href="https://boards.greenhouse.io/gongio">See open roles</a>'
    assert refs(html) == {"greenhouse": "gongio"}


def test_greenhouse_job_boards_host_and_junk_filter():
    html = '<a href="https://job-boards.greenhouse.io/telnyx54/jobs/4012345">Apply</a>'
    assert refs(html) == {"greenhouse": "telnyx54"}


def test_greenhouse_embed_script_is_not_the_slug_embed():
    html = (
        '<div id="grnhse_app"></div>'
        '<script src="https://boards.greenhouse.io/embed/job_board/js?for=chainalysis-careers">'
        "</script>"
    )
    assert refs(html) == {"greenhouse": "chainalysis-careers"}


def test_lever_link():
    html = '<a href="https://jobs.lever.co/outreach/">Careers</a>'
    assert refs(html) == {"lever": "outreach"}


def test_ashby_link_with_encoded_space():
    html = '<iframe src="https://jobs.ashbyhq.com/wealth-com"></iframe>'
    assert refs(html) == {"ashby": "wealth-com"}
    assert refs('<a href="https://jobs.ashbyhq.com/Sierra%20Space">jobs</a>') == {
        "ashby": "Sierra Space"
    }


def test_workable_link():
    html = '<link rel="canonical" href="https://apply.workable.com/blueground/" />'
    assert refs(html) == {"workable": "blueground"}


def test_smartrecruiters_link():
    html = '<a href="https://jobs.smartrecruiters.com/Devoteam/743999">Open positions</a>'
    assert refs(html) == {"smartrecruiters": "Devoteam"}


def test_workday_tenant_and_site_are_recorded_together():
    html = '<a href="https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite">Jobs</a>'
    assert refs(html) == {"workday": "nvidia.wd5/NVIDIAExternalCareerSite"}


def test_workday_without_locale_segment():
    html = "https://gong.wd12.myworkdayjobs.com/Gong"
    assert refs(html) == {"workday": "gong.wd12/Gong"}


def test_nothing_matches_means_nothing_returned():
    html = "<html><body><h1>We are hiring!</h1><p>Email us your resume.</p></body></html>"
    assert extract_board_refs(html) == []


def test_case_is_normalized_only_where_the_vendor_ignores_it():
    html = (
        '<a href="https://boards.greenhouse.io/GongIO">a</a>'
        '<a href="https://jobs.ashbyhq.com/OpenAI">b</a>'
    )
    assert refs(html) == {"greenhouse": "gongio", "ashby": "OpenAI"}


# Fetching -----------------------------------------------------------------


def test_candidate_urls_for_a_bare_domain():
    assert candidate_urls("www.gong.io/") == [
        "https://gong.io/careers",
        "https://gong.io/jobs",
        "https://gong.io/",
    ]
    assert candidate_urls("https://gong.io/company/careers") == [
        "https://gong.io/company/careers"
    ]


@respx.mock
def test_discover_reads_the_board_off_the_careers_page(client):
    respx.get("https://gong.io/careers").mock(
        return_value=httpx.Response(
            200, text='<a href="https://boards.greenhouse.io/gongio">Roles</a>'
        )
    )

    records = discover("gong.io", client, discovered_via="seed:test")

    assert len(records) == 1
    record = records[0]
    assert (record.vendor, record.slug) == ("greenhouse", "gongio")
    assert record.domain == "gong.io"
    assert record.board_url == "https://boards.greenhouse.io/gongio"
    assert record.status == "unverified"
    assert record.discovered_via == "seed:test"
    assert record.company_name == "Gong"


@respx.mock
def test_discover_follows_a_redirect_straight_to_the_board(client):
    respx.get("https://outreach.io/careers").mock(
        return_value=httpx.Response(302, headers={"location": "https://jobs.lever.co/outreach"})
    )
    respx.get("https://jobs.lever.co/outreach").mock(
        return_value=httpx.Response(200, text="<html>postings</html>")
    )

    records = discover("outreach.io", client)
    assert [(r.vendor, r.slug) for r in records] == [("lever", "outreach")]


@respx.mock
def test_discover_falls_through_dead_paths_and_gives_up_cleanly(client):
    respx.get("https://opaque.example/careers").mock(return_value=httpx.Response(404))
    respx.get("https://opaque.example/jobs").mock(return_value=httpx.Response(404))
    respx.get("https://opaque.example/").mock(
        return_value=httpx.Response(200, text="<h1>Join us</h1>")
    )

    assert discover("opaque.example", client) == []

    marker = no_match_record("opaque.example", discovered_via="seed:test")
    assert (marker.vendor, marker.slug, marker.domain) == (
        "unknown",
        "opaque.example",
        "opaque.example",
    )
    assert "no known ATS pattern" in marker.notes


# Probes -------------------------------------------------------------------


@respx.mock
def test_probe_lever_counts_postings(client):
    respx.get(POSTINGS_URL.format(slug="outreach")).mock(
        return_value=httpx.Response(200, json=[{"text": "AI GTM Architect"}, {"text": "AE"}])
    )
    probe = probe_org("lever", "outreach", client)
    assert (probe.status, probe.count) == ("ok", 2)
    assert "AI GTM Architect" in probe.sample


@respx.mock
def test_probe_workable_reads_name_and_jobs(client):
    respx.get(WORKABLE_API.format(slug="blueground")).mock(
        return_value=httpx.Response(
            200, json={"name": "Blueground", "jobs": [{"title": "RevOps Manager"}]}
        )
    )
    probe = probe_org("workable", "blueground", client)
    assert (probe.status, probe.count, probe.company_name) == ("ok", 1, "Blueground")


@respx.mock
def test_probe_smartrecruiters_zero_total_reads_as_dead(client):
    """The API answers totalFound 0 for unknown and empty alike. Recorded 2026-07-25."""
    respx.get(SMARTRECRUITERS_API.format(slug="ghost")).mock(
        return_value=httpx.Response(200, json={"totalFound": 0, "content": []})
    )
    assert probe_org("smartrecruiters", "ghost", client).status == "404"


@respx.mock
def test_probe_workday_posts_to_the_cxs_search(client):
    respx.post(
        "https://nvidia.wd5.myworkdayjobs.com/wday/cxs/nvidia/NVIDIAExternalCareerSite/jobs"
    ).mock(return_value=httpx.Response(200, json={"total": 2000, "jobPostings": [{"title": "X"}]}))

    probe = probe_org("workday", "nvidia.wd5/NVIDIAExternalCareerSite", client)
    assert (probe.status, probe.count) == ("ok", 2000)


def test_probe_workday_rejects_a_bare_slug(client):
    assert probe_org("workday", "gongio", client).status == "404"


@respx.mock
def test_probe_never_raises(client):
    respx.get(POSTINGS_URL.format(slug="flaky")).mock(side_effect=httpx.ConnectTimeout("boom"))
    assert probe_org("lever", "flaky", client).status == "error"
    assert probe_org("nonsense-vendor", "x", client).status == "error"
