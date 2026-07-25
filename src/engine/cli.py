"""Command line: engine run | engine check-org | engine digest | engine registry.

One command, run by cron, writes one file. That is the whole interface. The
registry subcommands are the exception: they maintain the org registry that
the nightly run reads, and they are run by hand or by their own cron.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

from engine import digest as digest_mod
from engine.config import HOST_DELAY_SECONDS, Config, data_dir, load_config
from engine.discover import add_domains, verify_registry
from engine.pipeline import check_org, run_pipeline
from engine.registry import VENDORS, Registry, registry_path
from engine.sourcers.base import PoliteClient


def _describe_config(cfg: Config) -> str:
    parts = [f"{name} from {origin}" for name, origin in sorted(cfg.sources.items())]
    return ", ".join(parts)


def cmd_run(args: argparse.Namespace) -> int:
    cfg = load_config()
    print(f"config: {_describe_config(cfg)}")
    orgs = cfg.companies.active()
    print(f"scanning {len(orgs)} orgs across ashby and greenhouse")

    with PoliteClient(delay=args.delay) as client:
        result = run_pipeline(
            cfg,
            client,
            use_state=not args.no_state,
            include_open=args.include_open,
            do_verify=not args.no_verify,
        )

    path = digest_mod.write(result, data_dir(), run_date=date.today())

    print(
        f"roles seen: {result.roles_seen} | "
        f"new and kept: {len(result.kept)} | flagged: {len(result.flagged)} | "
        f"still open: {result.still_open}"
    )
    if result.drop_counts:
        drops = ", ".join(f"{k}={v}" for k, v in sorted(result.drop_counts.items()))
        print(f"dropped: {drops}")
    for warning in result.warnings:
        print(f"warning: {warning.source}/{warning.slug}: {warning.reason}")
    print(f"digest: {path}")
    return 0


def cmd_check_org(args: argparse.Namespace) -> int:
    with PoliteClient(delay=args.delay) as client:
        findings = check_org(args.slug, client)

    found = False
    for f in findings:
        line = f"{f['source']:<11} {f['status']:<6} {f['detail']}"
        print(line)
        if f["status"] == "ok":
            found = True
            for title in f.get("sample", []):
                print(f"            - {title}")

    if not found:
        print(f"\nno board found for '{args.slug}' on either ATS.")
        print("the slug is not the company name. check the careers page board url.")
        return 1
    return 0


def cmd_registry_verify(args: argparse.Namespace) -> int:
    registry = Registry.load(registry_path())
    if not registry.records:
        print(f"registry is empty at {registry.path}. Seed it with: engine registry add-domains")
        return 1

    with PoliteClient(delay=args.delay) as client:
        counts = verify_registry(
            registry,
            client,
            vendor=args.vendor,
            limit=args.limit,
            progress=print,
        )

    print(
        f"checked: {counts['checked']} | live: {counts['live']} | dead: {counts['dead']} | "
        f"errors: {counts['error']} | went dead: {counts['went_dead']} | "
        f"revived: {counts['revived']}"
    )
    return 0


def cmd_registry_add_domains(args: argparse.Namespace) -> int:
    source = Path(args.file)
    if not source.is_file():
        print(f"no domain list at {source}")
        return 1
    domains = [line.strip() for line in source.read_text(encoding="utf-8").splitlines()]
    domains = [d for d in domains if d and not d.startswith("#")]

    registry = Registry.load(registry_path())
    with PoliteClient(delay=args.delay) as client:
        counts = add_domains(
            registry,
            domains,
            client,
            discovered_via=args.via or f"domains:{source.name}",
            progress=print,
        )

    print(
        f"scanned: {counts['scanned']} | already known: {counts['skipped']} | "
        f"orgs found: {counts['orgs_found']} | no match: {counts['no_match']}"
    )
    print("now verify what was found: uv run engine registry verify")
    return 0


def cmd_registry_stats(args: argparse.Namespace) -> int:
    registry = Registry.load(registry_path())
    stats = registry.stats()

    print(f"registry: {registry.path}")
    print(f"total orgs: {stats.total}")
    print("live by vendor:")
    for vendor in VENDORS:
        count = stats.live_by_vendor.get(vendor, 0)
        if count:
            print(f"  {vendor:<15} {count}")
    if not stats.live_by_vendor:
        print("  none yet")
    print(f"live: {stats.live} | dead: {stats.dead} | unverified: {stats.unverified}")
    print(f"domains with no detectable ATS: {stats.no_ats_found}")
    print(f"total postings last seen across live boards: {stats.postings_last_seen}")
    return 0


def cmd_digest(args: argparse.Namespace) -> int:
    """Reprint the digest for a date, without touching the network."""
    target = data_dir() / f"digest-{args.date}.md"
    if not target.is_file():
        print(f"no digest at {target}")
        return 1
    print(target.read_text(encoding="utf-8"))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="engine",
        description="candidate-zero: watch ATS boards, keep only what is verified live.",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=HOST_DELAY_SECONDS,
        help="seconds between requests to the same host (default: %(default)s)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="fetch every org, filter, and write tonight's digest")
    run.add_argument("--no-state", action="store_true", help="do not read or write data/seen.json")
    run.add_argument(
        "--include-open",
        action="store_true",
        help="include roles seen on a previous run, not just tonight's new ones",
    )
    run.add_argument("--no-verify", action="store_true", help="skip the liveness probe")
    run.set_defaults(func=cmd_run)

    check = sub.add_parser("check-org", help="probe both ATSs for a slug")
    check.add_argument("slug")
    check.set_defaults(func=cmd_check_org)

    dig = sub.add_parser("digest", help="print a digest that was already written")
    dig.add_argument("--date", default=date.today().isoformat())
    dig.set_defaults(func=cmd_digest)

    registry = sub.add_parser("registry", help="maintain the org registry, the engine's real asset")
    reg_sub = registry.add_subparsers(dest="registry_command", required=True)

    verify = reg_sub.add_parser("verify", help="re-check records against their vendor's public API")
    verify.add_argument("--vendor", choices=[v for v in VENDORS], default=None)
    verify.add_argument("--limit", type=int, default=None, help="check at most N records")
    verify.set_defaults(func=cmd_registry_verify)

    add = reg_sub.add_parser(
        "add-domains", help="discover boards for a newline-delimited domain list"
    )
    add.add_argument("file")
    add.add_argument("--via", default=None, help="discovered_via label written to new records")
    add.set_defaults(func=cmd_registry_add_domains)

    stats = reg_sub.add_parser("stats", help="print registry coverage")
    stats.set_defaults(func=cmd_registry_stats)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
