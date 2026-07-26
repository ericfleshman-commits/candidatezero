"""Render a RunResult to markdown.

The output is a file, on purpose. Files are greppable, diffable, and they do not
need a web app. Any future board can read the engine's output without sharing a
language with it.

House style, enforced by tests/test_public_hygiene.py: no em dashes and no arrow
characters anywhere in this repo, including in what it generates.
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from engine.config import REPO_ROOT
from engine.models import RunResult

TEMPLATE_DIR = REPO_ROOT / "templates"


def _day(value: datetime | None) -> str:
    return value.strftime("%Y-%m-%d") if value else "unknown"


def template_env(template_dir: Path | None = None) -> Environment:
    """The shared jinja environment. The newsletter renders through this too,
    so both public artifacts inherit the same strictness and filters."""
    env = Environment(
        loader=FileSystemLoader(template_dir or TEMPLATE_DIR),
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
    )
    env.filters["day"] = _day
    return env


def render(
    result: RunResult,
    run_date: date | None = None,
    template_dir: Path | None = None,
) -> str:
    template = template_env(template_dir).get_template("digest.md.j2")
    body = template.render(result=result, date=(run_date or date.today()).isoformat())
    # Collapse the blank-line drift that comes out of block templating.
    while "\n\n\n" in body:
        body = body.replace("\n\n\n", "\n\n")
    return body


def write(result: RunResult, data_dir: Path, run_date: date | None = None) -> Path:
    run_date = run_date or date.today()
    path = data_dir / f"digest-{run_date.isoformat()}.md"
    path.write_text(render(result, run_date), encoding="utf-8")
    return path
