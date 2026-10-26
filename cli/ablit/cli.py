"""Parse commands for planning and the Vast run lifecycle."""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Sequence

from ablit import __version__, exitcodes
from ablit.catalog import (
    AmbiguousModel,
    CatalogError,
    ModelNotInCatalog,
    check_entry,
    load_catalog,
    load_entry_file,
    resolve,
)
from ablit.backends import Backend, default_backends
from ablit.catalog.models import Entry
from ablit.catalog.staging import StagingError, compute_staging
from ablit.notices import SPEND_CAP_NOTICE, SPOT_WARNING
from ablit.plan import UnknownBackend, report_json, run_plan
from ablit.plan_text import render
from ablit.select import PlanOptions
from ablit.runs import RunError, RunStore
from ablit.select.request import request_from_entry

CONTRIBUTING_URL = "https://github.com/jameselkins/ablit/blob/main/CONTRIBUTING.md"

_RUN_HELP = "run ID. Defaults to the only active run."


def _add_selection_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("model", help="a catalog ID or a Hugging Face repo ID")
    parser.add_argument("--max-spend", type=float, metavar="USD",
                        help="spend at which ablit destroys the run. Counts compute, disk, "
                             "the model download and transfer charges.")
    parser.add_argument("--hours", type=float, default=1.0, metavar="N",
                        help="session length used to rank offers. It does not stop the run. Default 1.")
    parser.add_argument("--spot", action="store_true",
                        help="allow interruptible offers, after a warning. "
                             "Disabled until a spot round trip has been tested.")
    parser.add_argument("--min-download", type=float, metavar="MBPS",
                        help="floor on download speed. Default is the entry's value, or 500.")
    parser.add_argument("--trust", choices=("any", "verified"), default="any",
                        help="'verified' keeps only Vast verified hosts and RunPod Secure Cloud. "
                             "Clore has no verification, so it drops every Clore offer.")
    parser.add_argument("--min-reliability", type=float, metavar="X")
    parser.add_argument("--region", metavar="REGION")
    parser.add_argument("--max-price-hr", type=float, metavar="USD")
    parser.add_argument("--backends", metavar="vast,runpod,clore,lium",
                        help="limit the search to these backends. Output lists the backends left out and why.")
    parser.add_argument("--variant", metavar="ID", help="pick a build when a repo has several")
    parser.add_argument("--endpoint", choices=("tunnel", "public"), default="tunnel")
    parser.add_argument("--untested", action="store_true",
                        help="required to run `up` on an unverified entry")
    parser.add_argument("--catalog", metavar="PATH",
                        help="local catalog for testing. A paid launch from it also needs --untested.")
    parser.add_argument("--json", action="store_true", help="versioned machine-readable output")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ablit",
        description="Find the cheapest rented GPU machine that can run a model, "
                    "show the full cost, and start it on your own marketplace account.",
        epilog="Paid launches support the llama.cpp and Xolotl entries on Vast, RunPod, Clore and Lium. Other entries can be planned.",
    )
    parser.add_argument("--version", action="version", version=f"ablit {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="command")

    setup = sub.add_parser("setup", help="first-run account flow")
    setup.add_argument("backend", nargs="?", help="a marketplace: vast, runpod, clore or lium")
    setup.add_argument("--no-referral", action="store_true",
                       help="turn off all referral links, and save the setting")

    plan = sub.add_parser(
        "plan",
        help="show the cheapest qualifying offer. Rents nothing.",
        description="Search each marketplace, filter and rank the offers, and print the chosen offer and "
                    "the next four with their full cost. Exits with code 3 when no offer qualifies.",
    )
    _add_selection_flags(plan)

    up = sub.add_parser(
        "up",
        help="plan, confirm, rent, start the engine and print the endpoint",
        description="Run `plan`, show the quote, and ask you to type `rent`. Then rent the "
                    "machine, start the engine, check health, and print the endpoint.",
        epilog=SPEND_CAP_NOTICE + "\n" + SPOT_WARNING,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_selection_flags(up)
    up.add_argument("--another", action="store_true",
                    help="allow a second active run of the same model")
    up.add_argument("--yes", action="store_true",
                    help="approve the quote without typing rent, for agents and scripts. "
                         "The cap, the cap check and the price recheck still apply.")
    up.add_argument("--report", action="store_true",
                    help="write a local boot-test log with secrets removed")

    status = sub.add_parser("status", help="run state, resource IDs, spend against the cap")
    status.add_argument("run", nargs="?", help=_RUN_HELP)
    status.add_argument("--all", action="store_true", help="show every run")
    status.add_argument("--json", action="store_true")
    status.add_argument("--watch", action="store_true")
    status.add_argument("--show-key", action="store_true", help="print the endpoint key")

    logs = sub.add_parser("logs", help="stream logs from the box over SSH, with secrets removed")
    logs.add_argument("run", nargs="?", help=_RUN_HELP)
    logs.add_argument("--stage", choices=("bootstrap", "engine", "watchdog"))
    logs.add_argument("-f", dest="follow", action="store_true", help="keep streaming")

    tunnel = sub.add_parser("tunnel", help="re-open the SSH tunnel to a running endpoint")
    tunnel.add_argument("run", nargs="?", help=_RUN_HELP)

    down = sub.add_parser("down", help="destroy every billable resource the run created")
    down.add_argument("run", nargs="?", help=_RUN_HELP)

    catalog = sub.add_parser("catalog", help="list entries, explain one, or check a contributor's file")
    catalog_sub = catalog.add_subparsers(dest="catalog_command", metavar="list|show|validate")
    c_list = catalog_sub.add_parser("list", help="list entries and their test status")
    c_list.add_argument("--catalog", metavar="PATH", help="catalog directory. Default is the bundled one.")
    c_show = catalog_sub.add_parser("show", help="explain one entry")
    c_show.add_argument("id", help="a catalog ID or a Hugging Face repo ID")
    c_show.add_argument("--variant", metavar="ID")
    c_show.add_argument("--catalog", metavar="PATH")
    c_validate = catalog_sub.add_parser("validate", help="check a contributor's file")
    c_validate.add_argument("file", help="a catalog entry YAML file")
    return parser


def _not_implemented(command: str) -> int:
    print(f"ablit {command}: not implemented yet", file=sys.stderr)
    return exitcodes.NOT_IMPLEMENTED


def _load(path: str | None):
    try:
        return load_catalog(path)
    except CatalogError as error:
        print(f"ablit: {error}", file=sys.stderr)
        return None


def _report_problems(result) -> None:
    for name, problems in result.problems.items():
        print(f"ablit: {name} was skipped", file=sys.stderr)
        for problem in problems:
            print(f"  {problem}", file=sys.stderr)


def _cmd_catalog_list(args: argparse.Namespace) -> int:
    result = _load(args.catalog)
    if result is None:
        return exitcodes.USAGE
    rows = [(e.id, e.profile.engine, e.status, e.source.repo) for e in result.entries]
    headers = ("ID", "ENGINE", "STATUS", "SOURCE REPO")
    widths = [max(len(str(row[i])) for row in rows + [headers]) for i in range(len(headers))]
    for row in [headers, *rows]:
        print("  ".join(str(cell).ljust(widths[i]) for i, cell in enumerate(row)).rstrip())
    _report_problems(result)
    return exitcodes.INVALID_FILE if result.problems else exitcodes.OK


def _size_text(value: float | None, unit: str = "GB") -> str:
    return "unknown (sizes not filled in yet)" if value is None else f"{value:,.1f} {unit}"


def _describe(entry: Entry) -> str:
    p = entry.profile
    gpu = p.gpu
    lines = [
        f"{entry.id}",
        f"  {entry.display_name}",
        "",
        f"Status: {entry.status}",
        ("Hardware floors below come from the sources listed at the end. Only the machines in Tests have run it."
         if entry.tests else
         "Hardware floors below are not yet tested. They come from the sources listed at the end."),
        "",
        f"Source repo:  {entry.source.repo}",
        f"Revision:     {entry.source.revision}",
        f"Model license: {entry.source.license}",
        f"Modification: {entry.source.modification}",
    ]
    if entry.source.base_model:
        lines.append(f"Base model:   {entry.source.base_model}")
    lines += [
        "",
        f"Engine:       {p.engine} (lock {p.engine_lock})",
        f"GPUs:         {gpu.count} x {gpu.vendor}, at least {gpu.vram_gb_each:g} GB each"
        + (f", {gpu.vram_gb_total:g} GB in total" if gpu.vram_gb_total else ""),
    ]
    if gpu.compute_capability_min is not None:
        lines.append(f"Compute capability: {gpu.compute_capability_min:g} or higher")
    lines.append(f"Host RAM:     at least {p.host_ram_gb:g} GB"
                 + (f" ({p.host_ram_gb_recommended:g} GB recommended)" if p.host_ram_gb_recommended else ""))
    try:
        report = compute_staging(entry)
        download, peak = _size_text(report.download_gb), _size_text(report.peak_gb)
    except StagingError:
        download = peak = _size_text(None)
    lines += [
        f"Download:     {download}",
        f"Peak disk:    {peak} (computed from the staging steps, before the engine image)",
        f"Random-read storage required: {'yes' if p.storage.random_read_required else 'no'}",
        f"Context:      {p.context_tokens} tokens, {p.concurrency} request at a time, port {p.port}",
        f"Startup estimate: {p.startup_hours_estimate:g} hours after the download (an estimate)",
        f"Download speed floor: {p.min_download_mbps:g} Mbps",
        "",
        f"Artifacts:    {len(entry.artifacts)} file" + ("" if len(entry.artifacts) == 1 else "s"),
    ]
    if entry.tests:
        lines.append("Tests:        " + "; ".join(f"{t['backend']} on {t['date']}" for t in entry.tests))
    else:
        lines.append("Tests:        none. Nobody has booted this entry yet.")
    if entry.notes:
        lines += ["", "Notes:"] + [f"  - {note}" for note in entry.notes]
    lines += ["", "Sources:"]
    for item in entry.sources:
        lines.append(f"  - {item if isinstance(item, str) else item['url']}")
    return "\n".join(lines)


def _cmd_catalog_show(args: argparse.Namespace) -> int:
    result = _load(args.catalog)
    if result is None:
        return exitcodes.USAGE
    try:
        entry = resolve(result.entries, args.id, args.variant)
    except ModelNotInCatalog:
        print(f"ablit: {args.id} is not in the catalog. To add it, see {CONTRIBUTING_URL}", file=sys.stderr)
        return exitcodes.NOT_SUPPORTED
    except AmbiguousModel as error:
        print(f"ablit: {error}", file=sys.stderr)
        return exitcodes.USAGE
    except CatalogError as error:
        print(f"ablit: {error}", file=sys.stderr)
        return exitcodes.USAGE
    print(_describe(entry))
    return exitcodes.OK


def _cmd_catalog_validate(args: argparse.Namespace) -> int:
    path = Path(args.file)
    try:
        data = load_entry_file(path)
        problems = check_entry(data)
    except CatalogError as error:
        print(f"ablit: {error}", file=sys.stderr)
        return exitcodes.INVALID_FILE
    if problems:
        print(f"{path}: {len(problems)} problem(s)", file=sys.stderr)
        for problem in problems:
            print(f"  {problem}", file=sys.stderr)
        return exitcodes.INVALID_FILE
    print(f"{path}: ok (offline checks only. Run tools/verify_entry.py to check Hugging Face.)")
    return exitcodes.OK


def _plan_options(args: argparse.Namespace) -> PlanOptions | str:
    """Build the planner options from the flags, or return a message describing a bad flag."""
    if args.spot:
        return "--spot is disabled until a spot round trip has been tested"
    if not math.isfinite(args.hours) or not args.hours > 0:
        return "--hours must be above zero"
    for flag, value in (("--min-download", args.min_download), ("--max-price-hr", args.max_price_hr),
                        ("--max-spend", args.max_spend)):
        if value is not None and (not math.isfinite(value) or not value > 0):
            return f"{flag} must be above zero"
    if args.min_reliability is not None and not 0 <= args.min_reliability <= 1:
        return "--min-reliability must be between 0 and 1"
    return PlanOptions(
        hours=args.hours,
        spot=False,
        trust=args.trust,
        min_reliability=args.min_reliability,
        region=args.region.upper() if args.region else None,
        max_price_hr=args.max_price_hr,
        min_download_mbps=args.min_download,
    )


def _cmd_plan(
    args: argparse.Namespace,
    entry: Entry,
    backends: Mapping[str, Backend] | None,
    environ: Mapping[str, str] | None,
) -> int:
    opts = _plan_options(args)
    if isinstance(opts, str):
        print(f"ablit plan: {opts}", file=sys.stderr)
        return exitcodes.USAGE
    try:
        req = request_from_entry(entry)
    except StagingError as error:
        print(f"ablit plan: {entry.id} cannot be planned yet. {error}", file=sys.stderr)
        return exitcodes.USAGE
    owned = backends is None
    available = default_backends() if backends is None else backends
    try:
        report = run_plan(entry, req, opts, available, wanted=args.backends, max_spend=args.max_spend,
                          environ=environ)
    except UnknownBackend as error:
        print(f"ablit plan: {error}", file=sys.stderr)
        return exitcodes.USAGE
    finally:
        if owned:
            for backend in available.values():
                close = getattr(backend, "close", None)
                if close is not None:
                    close()
    if args.json:
        print(json.dumps(report_json(report), indent=2))
    else:
        print(render(report), end="")
    return exitcodes.OK if report.ranked else exitcodes.NOT_SUPPORTED


def _cmd_selection(
    args: argparse.Namespace,
    command: str,
    backends: Mapping[str, Backend] | None = None,
    environ: Mapping[str, str] | None = None,
) -> int:
    result = _load(args.catalog)
    if result is None:
        return exitcodes.USAGE
    try:
        entry = resolve(result.entries, args.model, args.variant)
    except ModelNotInCatalog:
        print(f"ablit: {args.model} is not in the catalog. To add it, see {CONTRIBUTING_URL}", file=sys.stderr)
        return exitcodes.NOT_SUPPORTED
    except CatalogError as error:
        print(f"ablit: {error}", file=sys.stderr)
        return exitcodes.USAGE
    if entry.status == "removed":
        print(f"ablit: {entry.id} was removed from the catalog and cannot be launched", file=sys.stderr)
        return exitcodes.NOT_SUPPORTED
    if command == "up":
        if args.max_spend is None:
            print("ablit up: --max-spend is required, and it has no default", file=sys.stderr)
            return exitcodes.USAGE
        if not args.yes and not (sys.stdin.isatty() and sys.stdout.isatty()):
            print("ablit up: a paid launch needs a terminal, because you must type `rent` to confirm. "
                  "Pass --yes to approve the quote without a terminal.", file=sys.stderr)
            return exitcodes.USAGE
        if entry.status == "unverified" and not args.untested:
            print(f"ablit up: {entry.id} is unverified. Pass --untested to run it anyway.", file=sys.stderr)
            return exitcodes.USAGE
    if command == "plan":
        return _cmd_plan(args, entry, backends, environ)
    from ablit.runs.up import up
    opts = _plan_options(args)
    if isinstance(opts, str):
        print(f"ablit up: {opts}", file=sys.stderr)
        return exitcodes.USAGE
    available = default_backends() if backends is None else backends
    try:
        return up(args, entry, opts, available["vast"], environ, RunStore(), drivers=dict(available))
    finally:
        if backends is None:
            for backend in available.values():
                backend.close()


def _main(
    argv: Sequence[str] | None = None,
    *,
    backends: Mapping[str, Backend] | None = None,
    environ: Mapping[str, str] | None = None,
) -> int:
    """Run the command line. Tests pass `backends` to control the HTTP calls and `environ` to set keys."""
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help(sys.stderr)
        return exitcodes.USAGE
    if args.command == "catalog":
        if args.catalog_command == "list":
            return _cmd_catalog_list(args)
        if args.catalog_command == "show":
            return _cmd_catalog_show(args)
        if args.catalog_command == "validate":
            return _cmd_catalog_validate(args)
        parser.parse_args(["catalog", "--help"])
        return exitcodes.USAGE
    if args.command in ("plan", "up"):
        return _cmd_selection(args, args.command, backends, environ)
    from ablit.credentials import setup_clore, setup_lium, setup_runpod, setup_vast
    from ablit.runs.existing import existing
    available = default_backends() if backends is None else backends
    try:
        if args.command == "setup":
            if args.backend not in (None, "vast", "clore", "runpod", "lium"):
                return _not_implemented("setup " + args.backend)
            if not sys.stdin.isatty():
                print("ablit setup needs a terminal.", file=sys.stderr)
                return 2
            if args.backend == "clore":
                return setup_clore(available["clore"], RunStore(), args.no_referral, environ)
            if args.backend == "lium":
                return setup_lium(available["lium"], RunStore(), args.no_referral, environ)
            if args.backend == "runpod":
                return setup_runpod(available["runpod"], RunStore(), args.no_referral, environ)
            return setup_vast(available["vast"], RunStore(), args.no_referral, environ)
        return existing(args, available["vast"], environ, RunStore(), drivers=dict(available))
    finally:
        if backends is None:
            for backend in available.values():
                backend.close()


def main(argv: Sequence[str] | None = None, *, backends=None, environ=None) -> int:
    from ablit.backends.errors import CredentialsRejected, WorkloadRejected
    from ablit.credentials import CredentialError
    try:
        return _main(argv, backends=backends, environ=environ)
    except (CredentialError, CredentialsRejected) as error:
        print(f"ablit: {error}", file=sys.stderr)
        return exitcodes.CREDENTIALS
    except WorkloadRejected:
        print("ablit: the marketplace rejected the workload. Run down to confirm cleanup.", file=sys.stderr)
        return exitcodes.WORKLOAD_REJECTED
    except RunError as error:
        print(f"ablit: {error}", file=sys.stderr)
        return exitcodes.USAGE
    except (EOFError, KeyboardInterrupt):
        print("Billing continues for existing rentals. Use `ablit status --all` and `ablit down <run>`.", file=sys.stderr)
        return exitcodes.USAGE
    except Exception as error:
        # BackendError, HTTP, OS and value errors land here, and so does any bug. Exit 1 promises
        # that no run is billing, so check the runs before using it.
        print(f"ablit: {type(error).__name__}: {error}", file=sys.stderr)
        return _unexpected_exit()


def _unexpected_exit() -> int:
    """Return 1 when no run could be billing, or else name the cleanup and return 7."""
    try:
        active = [r for r in RunStore().all() if r["state"] != "billing_stopped_confirmed"]
    except Exception:
        active = [None]
    if not active:
        return 1
    print("ablit: a run may still be billing. Check `ablit status --all` and run `ablit down <run>`.", file=sys.stderr)
    return exitcodes.CLEANUP_PENDING


if __name__ == "__main__":
    raise SystemExit(main())
