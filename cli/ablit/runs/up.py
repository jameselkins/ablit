"""Implement the paid `up` workflow: quote, confirm, rent and start a run."""

from __future__ import annotations

import hashlib
import math
import secrets
import subprocess
import sys
import time
import uuid
from dataclasses import asdict, replace

import httpx

from ablit import engines
from ablit.backends.base import ENV_KEY_NAMES, BootSpec
from ablit.backends.lium import LIUM_START
from ablit.backends.runpod import RUNPOD_START
from ablit.backends.errors import BackendError, CreateUncertain, CredentialsRejected, RequestRejected, WorkloadRejected
from ablit.catalog.loader import repo_root
from ablit.credentials import CredentialError, credentials, secret_set
from ablit.plan import report_json, run_plan
from ablit.plan_text import render
from ablit.runs import RunError
from ablit.runs.lifecycle import allocation, down
from ablit.runs.monitor import monitor
from ablit.runs.ssh import SSH
from ablit.select.cost import total_cost
from ablit.select.request import request_from_entry
from ablit.watchdog.ablit_watchdog import Config, reserve

# Vast supplies SSH in this launch mode and replaces the image entrypoint.
# https://docs.vast.ai/guides/instances/connect/overview
# The inspected server image already contains the bootstrap tools.
# On a rented machine sshd refused the run key with "bad ownership or modes for
# file /root/.ssh/authorized_keys", although the image itself has root-owned
# 700 directories. The modes come from how Vast writes the key at start, so the
# start script repairs them in the background for two minutes, which also
# covers a key Vast adds after this script runs.
ONSTART = ("umask 077; mkdir -p /ablit/logs /ablit/stages; "
           "(for i in $(seq 60); do if [ -f /root/.ssh/authorized_keys ]; then "
           "chown root:root /root /root/.ssh /root/.ssh/authorized_keys; "
           "chmod 700 /root /root/.ssh; chmod 600 /root/.ssh/authorized_keys; fi; "
           "sleep 2; done) >/dev/null 2>&1 &")


def minimum_cap(offer, req, floor) -> float:
    # RunPod reports no speed per host. The planner ranks such an offer at the floor, so the reserve does too.
    cfg = Config(0, offer.hourly_all_in, offer.bandwidth_cost_per_gb, 0, offer.download_mbps or floor)
    amount = total_cost(offer, req, 10 / 60, floor) + reserve(cfg)
    return math.ceil(amount * 100 - 1e-9) / 100


def make_intent(store, entry, offer, report, args, lock) -> dict:
    run_id = uuid.uuid4().hex
    root = repo_root()
    catalog_files = sorted((root / "catalog" / "models").glob("*.yaml"))
    catalog_digest = hashlib.sha256(b"".join(p.read_bytes() for p in catalog_files)).hexdigest()
    record = {
        "schema": 1, "run_id": run_id, "model_id": entry.id, "state": "creating",
        "backend": offer.backend,
        "quote": report_json(report), "offer": asdict(offer), "cap_usd": args.max_spend,
        "entry": entry.model_dump(), "catalog_sha256": catalog_digest,
        "image": lock["image"] + "@" + lock["digest"],
        "started_at": time.time(), "resource_ids": [], "create_sent": False,
        "min_download_mbps": args.min_download or entry.profile.min_download_mbps,
        "report_requested": getattr(args, "report", False),
        # RunPod and Clore have no key limited to one machine, so the local cap check does not wait for a stale heartbeat.
        "local_cap_primary": offer.backend in LOCAL_CAP_BACKENDS,
        "transfer_estimate_usd": offer.bandwidth_cost_per_gb * (report.req.download_gb + report.req.image_gb),
    }
    store.save(record)
    return record



def prepare(store, record, entry, driver, creds):
    """Finish idempotent setup after creation or label recovery."""
    found = driver.reconcile(record["run_id"], creds)
    if found.resource_ids:
        record["resource_ids"] = sorted(set(record["resource_ids"]) | set(found.resource_ids))
        store.save(record)
    if len(record["resource_ids"]) != 1:
        raise CreateUncertain("The instance has not been found by label. Re-run up or down; no create will be repeated.")
    if not record.get("watchdog_key_id"):
        if record.get("watchdog_key_sent"):
            raise RunError("The watchdog key response is unknown. Run down and review keys in the Vast console.")
        record["watchdog_key_sent"] = True
        store.save(record)
        try:
            watchdog = driver.watchdog_credential(allocation(record), creds)
        except RequestRejected as error:
            # A 4xx refusal means no key exists, so cleanup has nothing to revoke.
            # A 5xx answer may follow a created key, so it stays pending.
            if error.status is not None and 400 <= error.status < 500:
                record["watchdog_key_sent"] = False
                store.save(record)
            raise
        if watchdog is None:
            # The backend has no key limited to one machine. Nothing was created, so there is nothing to revoke.
            record["watchdog_key_sent"] = False
            store.save(record)
            return record
        record["watchdog_key_id"] = watchdog.key_id
        store.save(record)
        secret_set("watchdog/" + record["run_id"], watchdog.token)
    return record


RENTABLE = ("clore", "lium", "runpod", "vast")
# Backends with no key limited to one machine. The local cap check carries the cap on these.
LOCAL_CAP_BACKENDS = ("clore", "lium", "runpod")


def has_key(backend: str, environ) -> bool:
    try:
        credentials(backend, environ)
        return True
    except CredentialError:
        return False


def choose_backends(args, environ) -> list[str]:
    """Return the backends a paid `up` searches.

    With no `--backends`, ablit searches Vast as it always has, and also RunPod, Clore and Lium
    when a key for them exists. Keys for those with no Vast key search those alone.
    """
    wanted = [n.strip() for n in (args.backends or "").split(",") if n.strip()]
    if args.backends and (not wanted or any(n not in RENTABLE for n in wanted)):
        raise RunError("Paid launches support only --backends vast, runpod, clore, lium, or a comma-separated mix of them.")
    if wanted:
        return sorted(set(wanted))
    names = ["vast"]
    others = [n for n in ("runpod", "clore", "lium") if has_key(n, environ)]
    if others:
        names = names + others if has_key("vast", environ) else others
    return names



def _up(args, entry, opts, driver, environ, store, drivers=None):
    if entry.profile.engine not in engines.LAUNCHABLE_ENGINES or args.endpoint != "tunnel":
        raise RunError("Paid launches support the llama.cpp and Xolotl entries on Vast, RunPod, Clore and Lium through an SSH tunnel.")
    drivers = drivers or {"vast": driver}
    names = choose_backends(args, environ)
    missing = [n for n in names if n not in drivers]
    if missing:
        raise RunError(f"No driver is available for {', '.join(missing)}.")
    all_creds = {name: credentials(name, environ) for name in names}
    for name in names:
        drivers[name].store = store
    with store.lock("selection"):
        active = store.active_model(entry.id)
        if active and not args.another:
            if len(active) == 1 and active[0]["state"] == "creating":
                record = active[0]
                if record["cap_usd"] != args.max_spend or record["entry"] != entry.model_dump():
                    raise RunError("The existing run has a different cap or build. Run down first.")
                backend = record.get("backend", "vast")
                if backend not in drivers:
                    raise RunError(f"No driver is available for {backend}.")
                use_driver, use_creds = drivers[backend], credentials(backend, environ)
                use_driver.store = store
                with store.lock(record["run_id"]):
                    record = store.load(record["run_id"])
                    if record["state"] != "creating":
                        raise RunError("The run state changed. Check status before resuming.")
                    record = prepare(store, record, entry, use_driver, use_creds)
            else:
                raise RunError("An active run already exists for this model. Use tunnel, down, or --another.")
        else:
            lock = engines.engine_lock(entry)
            digest = lock.get("digest")
            import re
            if not isinstance(digest, str) or not re.fullmatch(r"sha256:[a-f0-9]{64}", digest):
                print("The engine image has no valid pinned digest.")
                return 5
            for name in names:
                drivers[name].denied_host_ids = store.denied_hosts()
            req = request_from_entry(entry, lock["image_gb"])
            report = run_plan(entry, req, opts, {name: drivers[name] for name in names}, wanted=",".join(names),
                              max_spend=args.max_spend,
                              environ={ENV_KEY_NAMES[name]: all_creds[name].api_key for name in names},
                              strict_credentials=True)
            denied = store.denied_hosts()
            report = replace(report, ranked=[r for r in report.ranked if r.offer.host_id not in denied])
            print(render(report), end="")
            if not report.ranked:
                return 3
            offer = report.ranked[0].offer
            use_driver, use_creds = drivers[offer.backend], all_creds[offer.backend]
            smallest = minimum_cap(offer, req, args.min_download or req.min_download_mbps)
            print(f"Smallest cap covering setup, ten minutes and the watchdog reserve: ${smallest:.2f}")
            if args.max_spend < smallest:
                return 5
            if args.catalog and not args.untested:
                raise RunError("A local catalog launch requires --untested.")
            engines.check_artifacts(entry)
            if args.yes:
                print("Quote approved with --yes.", flush=True)
            elif input("Type rent to rent this machine: ").strip() != "rent":
                print("Rental cancelled.")
                return 2
            again = use_driver.requote(offer, use_creds)
            # Remaining duration, reliability and measured speed drift between checks.
            # Every price, the hardware and the disk must match the approved quote.
            drift = {"remaining_hours": offer.remaining_hours, "reliability": offer.reliability,
                     "download_mbps": offer.download_mbps}
            if again is None or replace(again, **drift) != offer:
                print("The offer or price changed. Confirmation cancelled. Run up again for a new quote.")
                return 5
            if engines.engine_lock(entry) != lock:
                print("The image lock changed. Confirmation cancelled.")
                return 5
            record = make_intent(store, entry, offer, report, args, lock)
            print(f"Run: {record['run_id']}", flush=True)
            with store.lock(record["run_id"]):
                # Save the endpoint key before creating a billable resource.
                secret_set(record["run_id"], secrets.token_hex(32))
                ssh = SSH(store, record)
                public = ssh.identity_public()
                if hasattr(use_driver, "register_ssh_key"):
                    # Vast keeps the key on the account. RunPod takes it in the create request instead.
                    record["ssh_key_sent"] = True
                    store.save(record)
                    record["ssh_key_id"] = use_driver.register_ssh_key(public, use_creds)
                    store.save(record)
                # Clore runs this as the order's `command`, with its own entrypoint providing sshd. UNVERIFIED.
                boot = BootSpec(record["image"], {"runpod": RUNPOD_START, "lium": LIUM_START}.get(offer.backend, ONSTART),
                                math.ceil(offer.allocated_disk_gb), ssh_public_key=public)
                try:
                    alloc = use_driver.create(offer, boot, record["run_id"], use_creds)
                except (httpx.HTTPError, CreateUncertain):
                    print(f"Create response is unknown. Re-run up or `ablit down {record['run_id']}` to recover by label.")
                    return 7
                record = store.load(record["run_id"])
                record["resource_ids"] = alloc.resource_ids
                store.save(record)
                record = prepare(store, record, entry, use_driver, use_creds)
    return monitor(store, record, use_driver, use_creds, want_ready=True)


def up(args, entry, opts, driver, environ, store, drivers=None):
    before = {r["run_id"]: r["state"] for r in store.all()}
    try:
        return _up(args, entry, opts, driver, environ, store, drivers)
    except (CredentialError, CredentialsRejected, BackendError, httpx.HTTPError,
            RunError, ValueError, subprocess.SubprocessError) as error:
        targets = [r for r in store.active_model(entry.id)
                   if r["run_id"] not in before or (before[r["run_id"]] == "creating" and r.get("resource_ids"))]
        if not targets:
            raise
        print(f"ablit: {type(error).__name__}: {error}", file=sys.stderr)
        pending = False
        for record in targets:
            backend = record.get("backend", "vast")
            creds = credentials(backend, environ)
            with store.lock(record["run_id"]):
                pending = down(store, store.load(record["run_id"]), (drivers or {}).get(backend, driver), creds) == 7 or pending
        if pending:
            return 7
        if isinstance(error, (CredentialError, CredentialsRejected)):
            return 4
        if isinstance(error, WorkloadRejected):
            return 9
        return 6
