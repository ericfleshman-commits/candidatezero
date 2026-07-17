"""The run: fetch, filter, verify, diff, render.

Kept out of cli.py so the whole pipeline can be tested without argparse in the
way.

The expensive-last principle shows up twice here. Greenhouse detail fetches are
gated behind the title and location filters, and liveness probes only ever touch
roles that already survived every filter. A board like Gong's has 102 postings
and this run will make roughly two extra requests against it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from engine.config import Config, data_dir
from engine.filters import FilterEngine
from engine.models import OrgWarning, Role, RunResult
from engine.sourcers.ashby import AshbySourcer
from engine.sourcers.base import OrgNotFound, OrgUnavailable, PoliteClient
from engine.sourcers.greenhouse import GreenhouseSourcer
from engine.state import SeenStore
from engine.verify import verify


def build_sourcers(client: PoliteClient) -> dict:
    return {"ashby": AshbySourcer(client), "greenhouse": GreenhouseSourcer(client)}


def run_pipeline(
    cfg: Config,
    client: PoliteClient,
    root: Path | None = None,
    use_state: bool = True,
    include_open: bool = False,
    do_verify: bool = True,
    now: datetime | None = None,
) -> RunResult:
    stamp = now or datetime.now(UTC)
    filters = FilterEngine(cfg.filters)
    sourcers = build_sourcers(client)
    result = RunResult()

    all_roles: list[Role] = []
    scanned_org_keys: set[str] = set()

    for source, org in cfg.companies.active():
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
            result.closed = [
                f"{e.company_name}: {e.title} ({e.url})" for e in closed_entries
            ]

    survivors: list[Role] = []
    for role in all_roles:
        verdict = role.verdict = filters.evaluate(role)
        if verdict.decision == "drop":
            for reason in verdict.reasons:
                result.drop_counts[reason] = result.drop_counts.get(reason, 0) + 1
            continue
        survivors.append(role)

    for role in survivors:
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
        store.save(seen_path)

    return result


def check_org(slug: str, client: PoliteClient) -> list[dict]:
    """Probe both ATSs for a slug and report what answered.

    This is the doctor. Slugs are the single most common source of silent
    failure in this engine: they are not company names, they are whatever the
    org typed into its ATS on setup. Wealth.com is wealth-com, Gong is gongio,
    Telnyx is telnyx54.
    """
    from engine.sourcers.ashby import BOARD_URL as ASHBY_URL
    from engine.sourcers.greenhouse import LIST_URL as GH_URL

    probes = [
        ("ashby", ASHBY_URL.format(slug=slug)),
        ("greenhouse", GH_URL.format(slug=slug)),
    ]
    findings: list[dict] = []

    for source, url in probes:
        try:
            payload = client.get_json(url)
        except OrgNotFound:
            findings.append({"source": source, "status": "404", "detail": "no board at this slug"})
            continue
        except OrgUnavailable as exc:
            findings.append({"source": source, "status": "error", "detail": str(exc)})
            continue

        jobs = payload.get("jobs") or []
        listed = [j for j in jobs if j.get("isListed", True)]
        findings.append(
            {
                "source": source,
                "status": "ok",
                "detail": f"{len(listed)} listed jobs",
                "count": len(listed),
                "sample": [j.get("title", "") for j in listed[:3]],
            }
        )

    return findings
