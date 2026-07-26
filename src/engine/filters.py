"""Filter rules. Every decision in here comes from filters.yaml.

Order is title, then location, then comp. That order is not cosmetic: it is what
lets the Greenhouse sourcer skip a detail fetch for the 100 roles out of 102 that
were never candidates. Cheap checks first.

Nothing here drops a role quietly. Every drop carries a reason and every reason
is counted in the digest footer.
"""

from __future__ import annotations

import re

from engine.config import FiltersConfig
from engine.models import Role, Verdict

_PUNCT = re.compile(r"[^a-z0-9]+")
_GO_TO_MARKET = re.compile(r"\bgo to market\b")


def normalize_title(text: str) -> str:
    """Lowercase, depunctuate, and collapse every spelling of go-to-market to gtm.

    Stripping punctuation first turns "Go-To-Market" into "go to market", so the
    one collapse below catches the hyphenated and spaced spellings together.
    """
    low = _PUNCT.sub(" ", (text or "").lower()).strip()
    low = re.sub(r"\s+", " ", low)
    return re.sub(r"\s+", " ", _GO_TO_MARKET.sub("gtm", low)).strip()


def phrase_pattern(phrase: str) -> re.Pattern:
    """Compile a config phrase to a word-bounded, separator-tolerant regex.

    "ci/cd" must hit "CI/CD", "CI / CD", and "ci-cd"; "a/b test" must hit
    "A/B test". So the phrase is split into alphanumeric tokens joined by
    any run of non-alphanumerics. The lookarounds are hand-rolled word
    boundaries: "us" must not fire inside "status" and "ae" not inside "year".
    """
    tokens = [t for t in _PUNCT.split(phrase.lower()) if t]
    body = r"[^a-z0-9]+".join(re.escape(t) for t in tokens)
    return re.compile(rf"(?<![a-zA-Z0-9]){body}(?![a-zA-Z0-9])", re.IGNORECASE)


class FilterEngine:
    def __init__(self, filters: FiltersConfig):
        self.cfg = filters
        self.include = [normalize_title(p) for p in filters.title_families.include]
        self.flag_families = [normalize_title(p) for p in filters.title_families.flag]
        self.nyc_tokens = [t.lower() for t in filters.location.nyc_tokens]
        self.exclude_regions = [r.lower() for r in filters.location.remote_exclude_regions]
        self.us_signals = [phrase_pattern(t) for t in filters.location.us_signal_tokens]
        # State codes only count in the ", XX" position. Matched bare, Indiana's
        # "in" and Oregon's "or" would put half the English language in the US.
        self.us_states = [
            re.compile(rf",\s*{re.escape(c.lower())}(?![a-zA-Z0-9])", re.IGNORECASE)
            for c in filters.location.us_state_codes
        ]

    # Title ---------------------------------------------------------------

    def title_family(self, role: Role) -> str | None:
        """Returns "include", "flag", or None for no match."""
        title = normalize_title(role.title)
        if any(pattern and pattern in title for pattern in self.include):
            return "include"
        if any(pattern and pattern in title for pattern in self.flag_families):
            return "flag"
        return None

    # Location ------------------------------------------------------------

    @staticmethod
    def segments(role: Role) -> list[str]:
        """Both ATSs hand us pipe-separated locations, so one split covers both."""
        return [s.strip().lower() for s in (role.location_raw or "").split("|") if s.strip()]

    def _region_blocked(self, segment: str) -> bool:
        # Word boundaries matter. A bare "uk" substring would otherwise match
        # Ukraine, Fukuoka, and anything else with those two letters in it.
        return any(re.search(rf"\b{re.escape(r)}\b", segment) for r in self.exclude_regions)

    def _us_signal(self, segments: list[str]) -> bool:
        for segment in segments:
            if any(p.search(segment) for p in self.us_signals):
                return True
            if any(p.search(segment) for p in self.us_states):
                return True
        return False

    def location_status(self, role: Role) -> str:
        """Returns "ok", "remote-unverified", or "fail".

        "remote-unverified" is the closed gap: a role that is remote but shows
        no US signal anywhere in its location. It used to pass as if it were
        US remote. Now it survives location but carries a flag, because a role
        marked remote out of Lisbon is not a drop but is not trusted either.
        """
        segs = self.segments(role)

        for segment in segs:
            if any(token in segment for token in self.nyc_tokens):
                return "ok"

        if not self.cfg.location.allow_remote_us:
            return "fail"

        remote_segments = [s for s in segs if "remote" in s]
        if remote_segments:
            # "Remote (EMEA)" fails outright: that is a stated non-US region.
            if not any(not self._region_blocked(s) for s in remote_segments):
                return "fail"
            return "ok" if self._us_signal(segs) else "remote-unverified"

        # Ashby can mark a role remote without saying so in the location string.
        if role.is_remote and not any(self._region_blocked(s) for s in segs):
            return "ok" if self._us_signal(segs) else "remote-unverified"

        return "fail"

    def location_ok(self, role: Role) -> bool:
        """Location survives, verified or not. The gate for expensive work."""
        return self.location_status(role) != "fail"

    # Gate for expensive work ---------------------------------------------

    def wants_detail(self, role: Role) -> bool:
        """Is this role worth spending a Greenhouse detail request on?

        Title must match. Location normally must match too, but when
        kill_exception_comp_usd is configured a location miss can still be
        rescued by a big enough band, and we cannot know the band without the
        fetch. Title alone still removes about 98 percent of a board like Gong's.
        """
        if self.title_family(role) is None:
            return False
        if self.location_ok(role):
            return True
        return self.cfg.location.kill_exception_comp_usd is not None

    # Comp ----------------------------------------------------------------

    def _effective_max(self, role: Role) -> int | None:
        if role.comp.max is not None:
            return role.comp.max
        return role.comp.min

    # Verdict -------------------------------------------------------------

    def evaluate(self, role: Role) -> Verdict:
        reasons: list[str] = []

        family = self.title_family(role)
        if family is None:
            return Verdict(decision="drop", reasons=["title"])
        if family == "flag":
            role.flag("title-family-flag")
            reasons.append("adjacent title family, surfaced not trusted")

        floor = self.cfg.comp.floor_usd
        top = self._effective_max(role)
        exception = self.cfg.location.kill_exception_comp_usd

        location = self.location_status(role)
        if location == "fail":
            # Money can buy back a location miss, if the config says so.
            if exception is not None and top is not None and top >= exception:
                role.flag("location-exception-comp")
                reasons.append(f"outside target geography, band top clears {exception:,}")
            else:
                return Verdict(decision="drop", reasons=["location"])
        elif location == "remote-unverified":
            role.flag("remote-geo-unverified")
            reasons.append("remote with no US signal in the location")

        if not role.comp.known:
            role.flag("comp-unknown")
            reasons.append("no band published, worth a look anyway")
        elif top is not None and top >= floor:
            if role.comp.min is not None and role.comp.min >= floor:
                reasons.append(f"band clears {floor:,}")
            else:
                role.flag("band-top-only")
                reasons.append(f"only the top of the band clears {floor:,}")
        else:
            return Verdict(decision="drop", reasons=["comp"])

        decision = "flag" if role.flags else "pass"
        if decision == "pass" and not reasons:
            reasons.append("clean pass on title, location and comp")
        return Verdict(decision=decision, reasons=reasons)
