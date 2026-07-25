"""Core types.

The engine's contract with the outside world is files: JSON in, markdown out.
These models are the narrow waist in between. Every sourcer, whatever ATS it
talks to, hands back a list[Role] and nothing else.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

Source = Literal["ashby", "greenhouse", "lever"]
CompSource = Literal["structured", "parsed", "none"]
Decision = Literal["pass", "flag", "drop"]


class CompRange(BaseModel):
    """A salary band, and crucially where the number came from.

    An ATS that publishes a structured band is telling the truth on the record.
    A number scraped out of job description prose with a regex is an inference.
    Those are not the same fact and the digest never pretends they are.
    """

    min: int | None = None
    max: int | None = None
    currency: str | None = None
    source: CompSource = "none"

    @property
    def known(self) -> bool:
        return self.source != "none" and (self.min is not None or self.max is not None)

    def human(self) -> str:
        """Render the band for a human reading the digest at 7am."""
        if not self.known:
            return "not published"
        cur = "$" if (self.currency or "USD").upper() == "USD" else f"{self.currency} "
        if self.min is not None and self.max is not None:
            return f"{cur}{self.min:,} to {cur}{self.max:,}"
        only = self.min if self.min is not None else self.max
        bound = "from" if self.min is not None else "up to"
        return f"{bound} {cur}{only:,}"


class Verdict(BaseModel):
    """The filter's decision about a role, with its reasoning attached.

    Reasons are carried on pass as well as drop. A digest that says why it kept
    something is auditable. One that only says what it kept is a black box.
    """

    decision: Decision = "pass"
    reasons: list[str] = Field(default_factory=list)


class LivenessCheck(BaseModel):
    """Result of verify(role).

    The plan calls this a Verdict too. It is a different fact from the filter
    verdict above, so it gets its own name rather than overloading one.
    """

    live: bool
    checked_at: datetime
    method: str
    detail: str | None = None


class Role(BaseModel):
    """One job posting, normalized across ATSs."""

    id: str
    source: Source
    org_slug: str
    company_name: str
    title: str
    location_raw: str = ""
    is_remote: bool | None = None
    comp: CompRange = Field(default_factory=CompRange)
    url: str = ""
    published_at: datetime | None = None
    updated_at: datetime | None = None
    description_text: str | None = None
    flags: list[str] = Field(default_factory=list)
    verdict: Verdict | None = None

    @staticmethod
    def make_id(source: str, org_slug: str, external_id: str | int) -> str:
        """Stable across runs and unique across ATSs, which is what the seen-store needs.

        Greenhouse and Ashby both hand out integer-ish ids that collide happily.
        Namespacing by ats and org is what makes data/seen.json trustworthy.
        """
        return f"{source}:{org_slug}:{external_id}"

    # The plan names these fields flat on Role. They live on CompRange so the band
    # travels as one value, and are re-exposed here so callers can read either way.
    @property
    def comp_min(self) -> int | None:
        return self.comp.min

    @property
    def comp_max(self) -> int | None:
        return self.comp.max

    @property
    def comp_currency(self) -> str | None:
        return self.comp.currency

    @property
    def comp_source(self) -> CompSource:
        return self.comp.source

    def flag(self, name: str) -> None:
        if name not in self.flags:
            self.flags.append(name)


class OrgWarning(BaseModel):
    """A board that did not answer. Footer material, never a crash."""

    source: str
    slug: str
    reason: str


class RunResult(BaseModel):
    """Everything one run of the pipeline learned. The digest renders this."""

    orgs_scanned: int = 0
    roles_seen: int = 0
    kept: list[Role] = Field(default_factory=list)
    flagged: list[Role] = Field(default_factory=list)
    drop_counts: dict[str, int] = Field(default_factory=dict)
    warnings: list[OrgWarning] = Field(default_factory=list)
    closed: list[str] = Field(default_factory=list)
    # Survivors that are not new tonight. Counted, not printed, so a nightly
    # digest stays a diff rather than reprinting the same roles forever.
    still_open: int = 0
    # Registry accounting. orgs_live is every live org the registry holds;
    # orgs_without_harvester is the subset on vendors we cannot read yet.
    orgs_live: int = 0
    orgs_without_harvester: int = 0
    duration_seconds: float = 0.0

    def funnel_stages(self) -> list[tuple[str, int]]:
        """Eliminations in the order the funnel actually runs them.

        The order is a design constraint, not presentation: title and location
        are regex-cheap, comp parsing costs detail fetches, liveness costs a
        probe per role, and any future model call is the most expensive step of
        all. At registry scale this ordering is what keeps the engine viable.
        """
        return [
            ("title", self.drop_counts.get("title", 0)),
            ("location", self.drop_counts.get("location", 0)),
            ("comp", self.drop_counts.get("comp", 0)),
            ("liveness", self.drop_counts.get("zombie", 0)),
        ]

    @property
    def survivors(self) -> int:
        """Roles that cleared every funnel stage tonight, new or not."""
        return len(self.kept) + len(self.flagged) + self.still_open
