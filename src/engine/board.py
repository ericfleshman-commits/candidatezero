"""The public verified board: data/board/index.html.

One self-contained dark page, no external assets, no scripts. It renders only
what the public store holds and the run log's aggregate counts, which is the
same structural firewall the newsletter uses: private flags, suppression
history and the operator's own rules are not readable from here, so they
cannot leak from here.

The three sections are the product. NEW THIS WEEK and STILL OPEN are the
verified listing; CLOSED RECENTLY is the churn data nobody else publishes,
kept for a week by the store and then gone.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from jinja2 import Environment
from pydantic import BaseModel, Field

from engine.digest import template_env
from engine.public_store import PublicRole, PublicStore

VENDOR_LABELS = {
    "ashby": "Ashby",
    "greenhouse": "Greenhouse",
    "lever": "Lever",
    "workable": "Workable",
    "smartrecruiters": "SmartRecruiters",
    "workday": "Workday",
}

# A role is NEW for its first week on the board, trailing and inclusive,
# mirroring the newsletter's week window.
WEEK_DAYS = 7


class BoardRow(BaseModel):
    """One listing line. Employer data plus observed dates, nothing else."""

    company: str
    title: str
    comp_band: str = ""  # empty renders as "not published"
    location: str
    vendor: str
    posted: str
    url: str = ""
    verified: str = ""
    closed: str = ""
    days_open: int = 0
    # Cross-vendor duplicates folded into this line by the store.
    mirrors: int = 0


class BoardReport(BaseModel):
    """Everything the board template renders."""

    generated: str
    last_run: str
    boards_read: int = 0
    postings_read: int = 0  # the most recent run's count, not a weekly sum
    verified_live: int = 0
    closed_this_week: int = 0
    new_this_week: list[BoardRow] = Field(default_factory=list)
    still_open: list[BoardRow] = Field(default_factory=list)
    closed_recently: list[BoardRow] = Field(default_factory=list)


def location_label(entry: PublicRole) -> str:
    loc = entry.location or "not stated"
    if entry.remote and "remote" not in loc.lower():
        loc += " (remote)"
    return loc


def _day(value: datetime | None) -> str:
    return value.date().isoformat() if value else "not stated"


def _row(entry: PublicRole, mirrors: int) -> BoardRow:
    row = BoardRow(
        company=entry.company,
        title=entry.title,
        comp_band=entry.comp_band,
        location=location_label(entry),
        vendor=VENDOR_LABELS.get(entry.vendor, entry.vendor.title()),
        posted=_day(entry.posted_at),
        url=entry.url,
        verified=entry.last_verified.date().isoformat(),
        mirrors=mirrors,
    )
    if entry.closed_at is not None:
        row.closed = entry.closed_at.date().isoformat()
        row.days_open = max((entry.closed_at - entry.first_seen).days, 1)
    return row


def build_report(store: PublicStore, runs: list[dict], now: datetime) -> BoardReport:
    week_start = now.date() - timedelta(days=WEEK_DAYS - 1)

    mirror_counts: dict[str, int] = {}
    for entry in store.roles.values():
        if entry.collapsed_into is not None:
            mirror_counts[entry.collapsed_into] = mirror_counts.get(entry.collapsed_into, 0) + 1

    stamps = [e.last_verified for e in store.roles.values()]
    if stamps:
        last_run = max(stamps).strftime("%Y-%m-%d %H:%M UTC")
    elif runs:
        last_run = runs[-1]["date"]
    else:
        last_run = "never"

    report = BoardReport(
        generated=now.strftime("%Y-%m-%d %H:%M UTC"),
        last_run=last_run,
        boards_read=max((r.get("orgs_scanned", 0) for r in runs), default=0),
        # The last run's own count, never a sum: two runs in one week read
        # mostly the same postings, and adding them would inflate the stat.
        postings_read=runs[-1].get("postings_read", 0) if runs else 0,
    )

    for entry in store.open_roles():
        row = _row(entry, mirror_counts.get(entry.id, 0))
        if entry.first_seen.date() >= week_start:
            report.new_this_week.append(row)
        else:
            report.still_open.append(row)

    for entry in store.closed_roles():
        if entry.closed_at.date() >= week_start:
            report.closed_this_week += 1
            report.closed_recently.append(_row(entry, mirror_counts.get(entry.id, 0)))

    report.verified_live = len(report.new_this_week) + len(report.still_open)
    for section in (report.new_this_week, report.still_open, report.closed_recently):
        section.sort(key=lambda r: (r.company.lower(), r.title.lower()))
    return report


def html_env(template_dir: Path | None = None) -> Environment:
    # Same strict environment the digest and newsletter share, with HTML
    # escaping on: company names and titles are third-party text. The company
    # pages render through this too.
    env = template_env(template_dir)
    env.autoescape = True
    return env


def render(report: BoardReport, template_dir: Path | None = None) -> str:
    return html_env(template_dir).get_template("board.html.j2").render(report=report)


def write_stylesheet(board_dir: Path) -> None:
    # Strict CSP hosts (the portfolio blocks inline style) need the stylesheet
    # as a same-origin file, so it ships next to the page every render.
    css = Path(__file__).resolve().parent.parent.parent / "templates" / "board.css"
    if css.exists():
        (board_dir / "board.css").write_text(css.read_text(), encoding="utf-8")


def write(report: BoardReport, data_dir: Path) -> Path:
    path = data_dir / "board" / "index.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render(report), encoding="utf-8")
    write_stylesheet(path.parent)
    return path


def now_utc() -> datetime:
    return datetime.now(UTC)
