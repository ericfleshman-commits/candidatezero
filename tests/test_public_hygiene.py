"""The guard that keeps this repo publishable.

Two rules, both permanent:

1. Privacy. Nothing under config/private/ or data/ is ever tracked by git. The
   .gitignore is the first commit in this repo's history, but a .gitignore only
   stops accidents. This test stops the ones that get past it, for example a
   `git add -f` at 3am or a rename that lands real config in a public path.

2. Style. No em dashes and no arrow characters in any tracked text file. These
   are hard house rules for all public content in this repo.

The forbidden characters are written as escapes below rather than literally, for
the obvious reason that this file is itself a tracked text file.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

FORBIDDEN_PATHS = ("config/private/", "data/")

# Written as escapes on purpose. Spelled literally, this dict would make the file
# that enforces the rule the first file to break it.
FORBIDDEN_CHARS = {
    chr(0x2014): "em dash (U+2014)",
    chr(0x2192): "rightwards arrow (U+2192)",
    chr(0x2190): "leftwards arrow (U+2190)",
    chr(0x21D2): "rightwards double arrow (U+21D2)",
}

BINARY_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf", ".woff", ".woff2"}


def tracked_files() -> list[str]:
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return [p for p in result.stdout.split("\0") if p]


@pytest.fixture(scope="module")
def files() -> list[str]:
    try:
        return tracked_files()
    except (subprocess.CalledProcessError, FileNotFoundError):  # pragma: no cover
        pytest.skip("not a git checkout")


def test_no_private_config_or_data_is_tracked(files: list[str]) -> None:
    leaked = [p for p in files if any(p.startswith(prefix) for prefix in FORBIDDEN_PATHS)]
    assert leaked == [], (
        "The privacy firewall is breached. These paths are tracked by git and would "
        f"be published: {leaked}"
    )


def test_gitignore_still_covers_the_firewall() -> None:
    body = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
    for required in ("config/private/", "data/", ".env"):
        assert required in body, f".gitignore no longer ignores {required}"


def test_private_config_is_ignored_in_practice() -> None:
    """Belt and braces: ask git itself, rather than trusting a substring match."""
    probe = "config/private/filters.yaml"
    result = subprocess.run(
        ["git", "check-ignore", probe],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"git does not consider {probe} ignored"


def test_every_template_is_tracked_so_the_style_scan_sees_it(files: list[str]) -> None:
    """The style scan below walks git-tracked files. A template that never got
    added would render public artifacts while dodging the scan entirely."""
    on_disk = {f"templates/{p.name}" for p in (REPO_ROOT / "templates").iterdir() if p.is_file()}
    untracked = on_disk - set(files)
    assert untracked == set(), f"templates invisible to the style guard: {sorted(untracked)}"


def test_no_em_dashes_or_arrows_in_tracked_text(files: list[str]) -> None:
    violations: list[str] = []

    for rel in files:
        path = REPO_ROOT / rel
        if path.suffix.lower() in BINARY_SUFFIXES or not path.is_file():
            continue
        try:
            body = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for char, name in FORBIDDEN_CHARS.items():
            if char in body:
                line = body[: body.index(char)].count("\n") + 1
                violations.append(f"{rel}:{line} contains {name}")

    assert violations == [], "House style violated:\n" + "\n".join(violations)
