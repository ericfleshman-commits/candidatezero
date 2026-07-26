"""Config loading, and the privacy firewall it enforces.

Resolution is per file, not per directory: config/private/filters.yaml wins over
config/example/filters.yaml, while companies.yaml can still come from the example
set. That way a new clone runs immediately off the committed example config, and
Eric's real config overrides exactly the files he has bothered to write.

Nothing in this module ever writes to config/private/. It only reads.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field

REPO_ROOT = Path(__file__).resolve().parents[2]
PRIVATE_DIR = REPO_ROOT / "config" / "private"
EXAMPLE_DIR = REPO_ROOT / "config" / "example"
DATA_DIR = REPO_ROOT / "data"

USER_AGENT = "candidate-zero/0.1 (+https://github.com/ericfleshman-commits/candidate-zero)"
REQUEST_TIMEOUT = 20.0
HOST_DELAY_SECONDS = 0.5


class CompanyEntry(BaseModel):
    slug: str
    company: str | None = None
    note: str | None = None

    @property
    def display_name(self) -> str:
        return self.company or self.slug


class CompaniesConfig(BaseModel):
    ashby: list[CompanyEntry] = Field(default_factory=list)
    greenhouse: list[CompanyEntry] = Field(default_factory=list)
    dead: list[CompanyEntry] = Field(default_factory=list)

    def active(self) -> list[tuple[str, CompanyEntry]]:
        """Every org worth probing tonight, paired with its source. Dead orgs excluded."""
        return [("ashby", e) for e in self.ashby] + [("greenhouse", e) for e in self.greenhouse]


class TitleFamilies(BaseModel):
    include: list[str] = Field(default_factory=list)
    flag: list[str] = Field(default_factory=list)


class LocationRules(BaseModel):
    nyc_tokens: list[str] = Field(default_factory=list)
    allow_remote_us: bool = True
    remote_exclude_regions: list[str] = Field(default_factory=list)
    kill_exception_comp_usd: int | None = None
    # A remote role with none of these signals anywhere in its location is kept
    # and flagged remote-geo-unverified. Tokens match on word boundaries;
    # state codes only count in the ", XX" position so Indiana's "in" cannot
    # false-positive on "Remote in Ireland".
    us_signal_tokens: list[str] = Field(default_factory=list)
    us_state_codes: list[str] = Field(default_factory=list)


class CompRules(BaseModel):
    floor_usd: int = 0


class FiltersConfig(BaseModel):
    title_families: TitleFamilies = Field(default_factory=TitleFamilies)
    location: LocationRules = Field(default_factory=LocationRules)
    comp: CompRules = Field(default_factory=CompRules)
    # Shape rule packs: pack name to phrase list, matched case-insensitively
    # over title plus description. A hit flags, never drops.
    shapes: dict[str, list[str]] = Field(default_factory=dict)


class Config(BaseModel):
    companies: CompaniesConfig
    filters: FiltersConfig
    sources: dict[str, str] = Field(default_factory=dict)


def resolve(filename: str, root: Path | None = None) -> Path | None:
    """Private beats example. Returns None when neither exists."""
    base = root or REPO_ROOT
    candidates = (
        base / "config" / "private" / filename,
        base / "config" / "example" / filename,
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def _read_yaml(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def load_config(root: Path | None = None) -> Config:
    sources: dict[str, str] = {}
    loaded: dict[str, dict] = {}

    for name in ("companies.yaml", "filters.yaml"):
        path = resolve(name, root)
        if path is None:
            raise FileNotFoundError(
                f"No {name} found in config/private/ or config/example/. "
                "The example config is committed, so this usually means a bad checkout."
            )
        loaded[name] = _read_yaml(path)
        sources[name] = "private" if path.parent.name == "private" else "example"

    return Config(
        companies=CompaniesConfig(**loaded["companies.yaml"]),
        filters=FiltersConfig(**loaded["filters.yaml"]),
        sources=sources,
    )


def data_dir(root: Path | None = None) -> Path:
    d = (root or REPO_ROOT) / "data"
    d.mkdir(parents=True, exist_ok=True)
    return d
