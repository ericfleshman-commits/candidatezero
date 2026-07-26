"""The run: fetch, filter, verify, diff, render.

Kept out of cli.py so the whole pipeline can be tested without argparse in the
way.

The funnel order is enforced here and it is a scale constraint, not an
optimization: title-family and the cheap regex filters run before comp parsing
(Greenhouse detail fetches are gated behind them), comp runs before liveness
re-verification, and liveness runs before any model call this engine ever
grows. Every stage counts what it eliminated and the digest footer prints the
funnel, because at registry scale the eliminations ARE the product working.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path

from engine.config import CompanyEntry, Config, data_dir
from engine.dedupe import Deduper, HistoryConfig
from engine.filters import FilterEngine
from engine.models import OrgWarning, Role, RunResult, SuppressedRole
from engine.registry import Registry
from engine.sourcers.ashby import AshbySourcer
from engine.sourcers.base import OrgNotFound, OrgUnavailable, PoliteClient
from engine.sourcers.greenhouse import GreenhouseSourcer
from engine.sourcers.lever import LeverSourcer
from engine.state import SeenStore
from engine.verify import verify

# The vendors this engine can harvest tonight. The registry tracks more; a
# live workday org waits in the registry until its harvester exists.
HARVESTED_VENDORS: tuple[str, ...] = ("ashby", "greenhouse", "lever")


def build_sourcers(client: PoliteClient) -> dict:
    return {
        "ashby": AshbySourcer(client),
        "greenhouse": GreenhouseSourcer(client),
        "lever": LeverSourcer(client),
    }


def gather_orgs(
    cfg: Config, registry: Registry | None
) -> tuple[list[tuple[str, CompanyEntry]], int]:
    """The night's org list: companies.yaml pins first and always, then every
    live registry org whose vendor has a harvester.

    companies.yaml stays supported as a pin list on purpose. The registry is
    generated data; the pins are the orgs Eric refuses to lose to a bad
    verification pass. Returns the list plus a count of live orgs skipped
    because their vendor has no harvester yet.
    """
    orgs = list(cfg.companies.active())
    seen = {(source, entry.slug) for source, entry in orgs}
    without_harvester = 0

    if registry is not None:
        for record in registry.live():
            if record.vendor not in HARVESTED_VENDORS:
                without_harvester += 1
                continue
            key = (record.vendor, record.slug)
            if key in seen:
                continue
            seen.add(key)
            orgs.append(
                (record.vendor, CompanyEntry(slug=record.slug, company=record.company_name or None))
            )

    return orgs, without_harvester


def run_pipeline(
    cfg: Config,
    client: PoliteClient,
    root: Path | None = None,
    use_state: bool = True,
    include_open: bool = False,
    do_verify: bool = True,
    now: datetime | None = None,
    registry: Registry | None = None,
    history: HistoryConfig | None = None,
) -> RunResult:
    stamp = now or datetime.now(UTC)
    filters = FilterEngine(cfg.filters)
    deduper = Deduper(history.entries) if history and history.entries else None
    sourcers = build_sourcers(client)
    result = RunResult()

    orgs, result.orgs_without_harvester = gather_orgs(cfg, registry)
    result.orgs_live = len(registry.live()) if registry is not None else 0

    all_roles: list[Role] = []
    scanned_org_keys: set[str] = set()

    for source, org in orgs:
        sourcer = sourcers[source]
        try:
            roles = sourcer.fetch(org, needs_detail=filters.wants_detail)
        except OrgNotFound:
            result.warnings.append(
                OrgWarning(
                    source=source,
                    slug=org.slug,
                    reason="404, board not found. Slug is wrong or the org left this ATS.",
                )
            )
            continue
        except OrgUnavailable as exc:
            # A dead org must never crash the run. It goes in the footer.
            result.warnings.append(
                OrgWarning(source=source, slug=org.slug, reason=f"unavailable: {exc}")
            )
            continue

        result.orgs_scanned += 1
        scanned_org_keys.add(f"{source}:{org.slug}")
        all_roles.extend(roles)

    result.roles_seen = len(all_roles)
    board_ids = {r.id for r in all_roles}

    # Churn accounting happens against every role the boards showed us, before
    # any filtering, so a role that merely dipped below the comp floor is not
    # mistaken for a role that closed.
    new_ids: set[str] = board_ids
    store: SeenStore | None = None
    seen_path: Path | None = None
    if use_state:
        seen_path = data_dir(root) / "seen.json"
        store = SeenStore.load(seen_path)
        first_run = not store.roles
        new_ids, closed_entries = store.reconcile(all_roles, scanned_org_keys, now=stamp)
        if not first_run:
            result.closed = [f"{e.company_name}: {e.title} ({e.url})" for e in closed_entries]

    # Funnel stages 1 to 3: title, then location, then comp. FilterEngine
    # evaluates in exactly that order and its drop reason names the stage that
    # killed the role, which is where the per-stage counters come from. Comp
    # parsing itself already ran expensive-last: the Greenhouse sourcer only
    # fetched detail for roles that cleared title and location.
    survivors: list[Role] = []
    for role in all_roles:
        verdict = role.verdict = filters.evaluate(role)
        if verdict.decision == "drop":
            for reason in verdict.reasons:
                result.drop_counts[reason] = result.drop_counts.get(reason, 0) + 1
            continue
        survivors.append(role)

    # Between comp and liveness sits dedupe: a dict lookup against history,
    # cheaper than any probe. A role the owner already ruled on is suppressed
    # here, before the engine spends a request verifying it, and lands in the
    # digest's Suppressed section rather than vanishing. It is checked every
    # night, not just when new, because a standing ruling has no expiry.
    for role in survivors:
        if deduper is not None:
            entry = deduper.match(role)
            if entry is not None:
                result.suppressed.append(
                    SuppressedRole(
                        company=role.company_name,
                        title=role.title,
                        status=entry.status,
                        date=entry.date,
                        reason=entry.reason,
                    )
                )
                continue
            note = deduper.company_note(role)
            if note is not None:
                # Live role at a company with history: surfaced, but flagged.
                role.flag("history-at-company")
                role.verdict.reasons.append(note)

        # Funnel stage 4: liveness, and only for roles that survived every
        # filter. Any model call this engine ever grows belongs after this line.
        if do_verify:
            liveness = verify(role, client, board_ids=board_ids)
            if not liveness.live:
                result.drop_counts["zombie"] = result.drop_counts.get("zombie", 0) + 1
                continue

        is_new = role.id in new_ids
        if not is_new and not include_open:
            result.still_open += 1
            continue
        if role.flags:
            result.flagged.append(role)
        else:
            result.kept.append(role)

    if store is not None and seen_path is not None:
        # Only clean survivors get public-report fields. Flagged roles carry
        # the operator's private rules and never reach the store's public side.
        store.record_public(result.kept)
        store.save(seen_path)

    return result


def append_run_record(
    result: RunResult, root: Path | None = None, run_date: date | None = None
) -> Path:
    """One JSON line per run to data/runs.jsonl.

    This is the engine's own churn data: what it read, what each funnel stage
    killed, and what survived. The newsletter sprint consumes this file; the
    contract here is just to write it honestly.
    """
    path = data_dir(root) / "runs.jsonl"
    record = {
        "date": (run_date or date.today()).isoformat(),
        "orgs_live": result.orgs_live,
        "orgs_scanned": result.orgs_scanned,
        "postings_read": result.roles_seen,
        "eliminated": dict(result.funnel_stages()),
        "survivors": result.survivors,
        "flagged": len(result.flagged),
        "suppressed": len(result.suppressed),
        "zombies": result.drop_counts.get("zombie", 0),
        "dead_orgs": len(result.warnings),
        "duration_seconds": round(result.duration_seconds, 1),
    }
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")
    return path


def check_org(slug: str, client: PoliteClient) -> list[dict]:
    """Probe every ATS we know for a slug and report what answered.

    This is the doctor. Slugs are the single most common source of silent
    failure in this engine: they are not company names, they are whatever the
    org typed into its ATS on setup. Wealth.com is wealth-com, Gong is gongio,
    Telnyx is telnyx54.

    Workday is only probed when the slug carries its tenant/site shape, since a
    bare slug cannot address a Workday board at all.
    """
    from engine.discover import PROBE_VENDORS, probe_org

    findings: list[dict] = []
    for vendor in PROBE_VENDORS:
        if vendor == "workday" and "/" not in slug:
            continue
        probe = probe_org(vendor, slug, client)
        findings.append(
            {
                "source": vendor,
                "status": probe.status,
                "detail": probe.detail if probe.status != "404" else "no board at this slug",
                "count": probe.count,
                "sample": probe.sample,
            }
        )
    return findings
