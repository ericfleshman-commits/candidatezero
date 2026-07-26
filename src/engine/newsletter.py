"""The weekly public report: data/newsletter-YYYY-Www.md.

The waitlist is the newsletter, the newsletter is the engine's nightly exhaust.
This module folds the trailing week of runs.jsonl and seen-store churn into one
markdown file the founder can paste anywhere. No sending, no site; the
deliverable is the file.

The privacy firewall here is structural, not editorial. This module reads only
the public fields of the seen store and the run log's aggregate counts. Filter
verdicts, flags, suppression history, and the operator's own comp rules never
reach it, so they cannot leak from it. The one public sentence about filtering
stays generic on purpose: senior comp floors, nothing personal.

The CLOSED section is the closure loop made public: seen-store entries whose
closed_at landed this week, with days alive. The same closed_at signal is the
future per-user "closure by design" feature from the investor narrative.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

from pydantic import BaseModel, Field

from engine.digest import template_env
from engine.state import SeenStore


class NewRole(BaseModel):
    """One publishable verified role. Every field is the employer's own data."""

    company: str
    title: str
    comp_band: str
    location: str
    url: str


class ClosedRole(BaseModel):
    company: str
    title: str
    days_alive: int


class WeekReport(BaseModel):
    """Everything the newsletter template renders. Aggregates and public
    fields only; nothing in here can name a private rule."""

    week_label: str
    start: date
    end: date
    postings_read: int = 0
    orgs_scanned: int = 0
    verified_live: int = 0
    ghosts: int = 0
    new_roles: list[NewRole] = Field(default_factory=list)
    # Previously verified roles that vanished this week, listed by name.
    closed: list[ClosedRole] = Field(default_factory=list)
    # Every board departure this week, verified or not. Count only.
    total_closed: int = 0


def week_label(week_end: date) -> str:
    year, week, _ = week_end.isocalendar()
    return f"{year}-W{week:02d}"


def read_runs(path: Path, week_end: date) -> list[dict]:
    """The trailing week of run records, one per date, oldest first.

    A rerun on the same night supersedes the earlier line; summing both would
    double-count every posting. Unparseable lines are skipped, not fatal: a
    weekly report should survive one mangled record in an append-only log.
    """
    start = week_end - timedelta(days=6)
    by_date: dict[date, dict] = {}
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
                day = date.fromisoformat(record["date"])
            except (ValueError, KeyError, TypeError):
                continue
            if start <= day <= week_end:
                by_date[day] = record
    return [by_date[day] for day in sorted(by_date)]


def build_report(store: SeenStore, runs: list[dict], week_end: date) -> WeekReport:
    start = week_end - timedelta(days=6)
    report = WeekReport(week_label=week_label(week_end), start=start, end=week_end)

    report.postings_read = sum(r.get("postings_read", 0) for r in runs)
    report.orgs_scanned = max((r.get("orgs_scanned", 0) for r in runs), default=0)
    # Older records predate the explicit zombies field; fall back to the funnel.
    report.ghosts = sum(
        r.get("zombies", r.get("eliminated", {}).get("liveness", 0)) or 0 for r in runs
    )
    report.verified_live = runs[-1].get("survivors", 0) if runs else 0

    for entry in store.roles.values():
        if entry.closed_at is not None:
            if start <= entry.closed_at.date() <= week_end:
                report.total_closed += 1
                if entry.public:
                    days = max((entry.closed_at - entry.first_seen).days, 1)
                    report.closed.append(
                        ClosedRole(
                            company=entry.company_name, title=entry.title, days_alive=days
                        )
                    )
        elif entry.public and start <= entry.first_seen.date() <= week_end:
            report.new_roles.append(
                NewRole(
                    company=entry.company_name,
                    title=entry.title,
                    comp_band=entry.comp_band,
                    location=entry.location,
                    url=entry.url,
                )
            )

    report.new_roles.sort(key=lambda r: (r.company, r.title))
    report.closed.sort(key=lambda r: (r.company, r.title))
    return report


def render(report: WeekReport, template_dir: Path | None = None) -> str:
    template = template_env(template_dir).get_template("newsletter.md.j2")
    body = template.render(report=report)
    while "\n\n\n" in body:
        body = body.replace("\n\n\n", "\n\n")
    return body


def write(report: WeekReport, data_dir: Path) -> Path:
    path = data_dir / f"newsletter-{report.week_label}.md"
    path.write_text(render(report), encoding="utf-8")
    return path
