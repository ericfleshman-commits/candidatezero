"""Dedupe against application history. The engine never resurfaces a decision.

Exhibit A, 2026-07-16: the first digest surfaced "Manifest: GTM Engineer" as a
clean PASS. Its owner had manually disqualified that exact role the same day as
a software engineer role in costume. An engine that resurfaces a role you
already ruled out is not saving you time, it is quietly undoing your judgment.
This module is the guarantee that never happens again.

History lives in config/private/history.yaml, gitignored, because where you
applied and who rejected you is nobody's business but yours. The committed
example documents the schema with invented companies.

Two suppression scopes, decided by status:

- dq and blacklist are company-level. Every role from that company is
  suppressed, at any title, for any money.
- applied, interviewing and rejected are role-level. Only the matching role is
  suppressed; a different role at the same company stays live and carries an
  informational history-at-company flag, because a second role at a company
  already talking to you is a signal, not noise.
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from engine.config import resolve
from engine.filters import normalize_title
from engine.models import Role

Status = Literal["applied", "interviewing", "rejected", "dq", "blacklist"]

# Statuses that suppress every role from the company, not just the matching one.
COMPANY_WIDE: frozenset[str] = frozenset({"dq", "blacklist"})

# Legacy flat-list schema: key in the yaml, status each name maps to.
_LEGACY_KEYS: tuple[tuple[str, Status], ...] = (
    ("applied", "applied"),
    ("interviewing", "interviewing"),
    ("rejected", "rejected"),
    ("blacklist_companies", "blacklist"),
)

_PUNCT = re.compile(r"[^a-z0-9]+")

# Trailing tokens that are branding, not identity. "Wealth.com" and "Wealth"
# are the same company; so are "Tonic AI" and "Tonic", "Listen Labs" and
# "Listen". Stripped from the end only, and never down to an empty name.
_SUFFIX_TOKENS = frozenset({"inc", "labs", "hq", "com", "ai", "app", "io", "co"})

# Leading tokens that are domain-name costume, not identity. Exhibit B,
# 2026-07-26: Profound's board lives at the slug "Tryprofound", so the digest
# flagged a company its owner had already applied to. "Try", "get" and friends
# are how startups dress a taken name into an available domain.
_PREFIX_TOKENS = frozenset({"try", "get", "join", "use", "hey", "the"})

# Glued forms ("Tryprofound", "ProfoundHQ") are only unstuck when what remains
# is at least this long. The guard is what keeps Theory, Getty, Heyday, Useful
# and Cisco being themselves rather than ory, ty, day, ful and cis.
_MIN_STEM = 4


def _unstick(token: str, prefixes: frozenset[str], suffixes: frozenset[str]) -> str:
    for prefix in prefixes:
        if token.startswith(prefix) and len(token) - len(prefix) >= _MIN_STEM:
            token = token[len(prefix) :]
            break
    for suffix in suffixes:
        if token.endswith(suffix) and len(token) - len(suffix) >= _MIN_STEM:
            token = token[: -len(suffix)]
            break
    return token


def normalize_company(name: str) -> str:
    """Lowercase, depunctuate, drop branding prefixes and suffixes.

    Token-level first ("The Trade Desk", "Profound HQ"), then glued
    ("Tryprofound", "ProfoundHQ"). Both sides of every comparison go through
    here, so an over-strip stays consistent and still matches itself; the
    _MIN_STEM guard is what keeps over-strips rare. Never strips to nothing.
    """
    tokens = _PUNCT.sub(" ", (name or "").lower()).split()
    while len(tokens) > 1 and tokens[-1] in _SUFFIX_TOKENS:
        tokens.pop()
    while len(tokens) > 1 and tokens[0] in _PREFIX_TOKENS:
        tokens.pop(0)
    if len(tokens) == 1:
        tokens[0] = _unstick(tokens[0], _PREFIX_TOKENS, _SUFFIX_TOKENS)
    return " ".join(tokens)


def title_matches(entry_title: str, role_title: str) -> bool:
    """Same title family: one normalized title contains the other.

    Containment rather than equality so "GTM Engineer" in the history matches
    "Senior GTM Engineer" and "GTM Engineer, Seller Efficiency" on a board.
    Normalization reuses the filter's rules, so every spelling of go-to-market
    lands on gtm before comparison.
    """
    a = normalize_title(entry_title)
    b = normalize_title(role_title)
    if not a or not b:
        return False
    return a in b or b in a


class HistoryEntry(BaseModel):
    """One ruling: a company, what happened there, and why.

    role is optional on purpose. Without one the entry matches every role at
    the company, because a ruling you cannot scope to a title is a ruling on
    the company. With one, role-level statuses scope to that title family.

    Unknown keys are an error, not a shrug. A flow-style yaml entry with an
    unquoted comma in its reason parses into bogus extra keys; swallowing them
    would silently truncate a ruling, which is the exact failure this module
    exists to prevent.
    """

    model_config = ConfigDict(extra="forbid")

    company: str
    status: Status
    role: str | None = None
    date: str | None = None
    reason: str | None = None

    @field_validator("date", mode="before")
    @classmethod
    def _date_to_iso(cls, value: object) -> object:
        # yaml parses a bare 2026-07-16 into a date object. It is display data
        # here, so it travels as a string.
        if isinstance(value, dt.date):
            return value.isoformat()
        return value


class HistoryConfig(BaseModel):
    entries: list[HistoryEntry] = Field(default_factory=list)
    source: str = "none"


def parse_history(raw: dict) -> list[HistoryEntry]:
    """Read both schemas: structured entries and the legacy flat lists."""
    entries = [HistoryEntry(**item) for item in raw.get("history") or []]
    for key, status in _LEGACY_KEYS:
        for name in raw.get(key) or []:
            entries.append(HistoryEntry(company=name, status=status))
    return entries


def load_history(root=None) -> HistoryConfig:
    """Private beats example, like every other config file. Missing is empty,
    not an error: the engine ran without history for its whole first week."""
    path = resolve("history.yaml", root)
    if path is None:
        return HistoryConfig()
    with path.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    origin = "private" if path.parent.name == "private" else "example"
    return HistoryConfig(entries=parse_history(raw), source=origin)


class Deduper:
    """Answers one question per role: did the owner already rule on this?"""

    def __init__(self, entries: list[HistoryEntry]):
        self.by_company: dict[str, list[HistoryEntry]] = {}
        for entry in entries:
            key = normalize_company(entry.company)
            if key:
                self.by_company.setdefault(key, []).append(entry)

    def _entries_for(self, role: Role) -> list[HistoryEntry]:
        return self.by_company.get(normalize_company(role.company_name), [])

    def match(self, role: Role) -> HistoryEntry | None:
        """The entry that suppresses this role, or None to let it live.

        Company-level statuses are checked first: a blacklist outranks an
        applied entry at the same company, whatever their file order.
        """
        entries = self._entries_for(role)
        for entry in entries:
            if entry.status in COMPANY_WIDE:
                return entry
        for entry in entries:
            if entry.role is None or title_matches(entry.role, role.title):
                return entry
        return None

    def company_note(self, role: Role) -> str | None:
        """Context for a role that stays live at a company with history."""
        entries = self._entries_for(role)
        if not entries:
            return None
        entry = entries[0]
        when = f" {entry.date}" if entry.date else ""
        return f"history at this company: {entry.status}{when}"
