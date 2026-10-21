"""Implement the paid Vast workflow and commands that inspect existing runs."""

from __future__ import annotations

import hashlib
import contextlib
import json
import math
import secrets
import subprocess
import sys
import time
import uuid
from dataclasses import asdict, replace

import httpx

from ablit import engines
from ablit.backends.base import BootSpec
from ablit.backends.errors import BackendError, CreateUncertain, CredentialsRejected, RequestRejected, WorkloadRejected
from ablit.catalog.loader import repo_root
from ablit.credentials import CredentialError, credentials, secret_get, secret_set, setup_vast
from ablit.plan import report_json, run_plan
from ablit.plan_text import render
from ablit.runs import RunError, RunStore
from ablit.runs.lifecycle import allocation, down, local_stop_needed, refresh, run_redactor
from ablit.runs.ssh import SSH
from ablit.select.cost import total_cost
from ablit.select.request import request_from_entry
from ablit.watchdog.ablit_watchdog import Config, reserve

# Vast supplies SSH in this launch mode and replaces the image entrypoint.
# https://docs.vast.ai/guides/instances/connect/overview
# The inspected server image already contains the bootstrap tools.
ONSTART = "umask 077; mkdir -p /ablit/logs /ablit/stages"
POLL_SECONDS = 10


def minimum_cap(offer, req, floor) -> float:
    cfg = Config(0, offer.hourly_all_in, offer.bandwidth_cost_per_gb, 0, offer.download_mbps)
    amount = total_cost(offer, req, 10 / 60, floor) + reserve(cfg)
    return math.ceil(amount * 100 - 1e-9) / 100


def endpoint(record: dict) -> None:
    port, run_id = record["local_port"], record["run_id"]
    print(f"Endpoint: http://127.0.0.1:{port}/v1   (through an SSH tunnel to the rented machine)")
    print(f"API key:  saved in your keyring as ablit/{run_id}. Run `ablit status {run_id} --show-key` to print it.")
    print(f'curl http://127.0.0.1:{port}/v1/chat/completions -H "Authorization: Bearer $ABLIT_KEY" \\\n'
          '  -H "Content-Type: application/json" \\\n'
          f'''  -d '{{"model":"{record['model_id']}","messages":[{{"role":"user","content":"Hello"}}],"max_tokens":8}}' ''')
    print(f"Closing this tunnel doesn't stop billing. Run `ablit down {run_id}` to stop it.", flush=True)


def make_intent(store, entry, offer, report, args, lock) -> dict:
    run_id = uuid.uuid4().hex
    root = repo_root()
    catalog_files = sorted((root / "catalog" / "models").glob("*.yaml"))
    catalog_digest = hashlib.sha256(b"".join(p.read_bytes() for p in catalog_files)).hexdigest()
    record = {
        "schema": 1, "run_id": run_id, "model_id": entry.id, "state": "creating", "backend": "vast",
        "quote": report_json(report), "offer": asdict(offer), "cap_usd": args.max_spend,
        "entry": entry.model_dump(), "catalog_sha256": catalog_digest,
        "image": lock["image"] + "@" + lock["digest"],
        "started_at": time.time(), "resource_ids": [], "create_sent": False,
        "min_download_mbps": args.min_download or entry.profile.min_download_mbps,
        "report_requested": getattr(args, "report", False),
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
        record["watchdog_key_id"] = watchdog.key_id
        store.save(record)
        secret_set("watchdog/" + record["run_id"], watchdog.token)
    return record


def monitor(store, record, driver, creds, *, want_ready=False):
    """Poll while up, status --watch or tunnel remains attached."""
    run_id = record["run_id"]
    tunnel = None
    uploaded = False
    stage = None
    try:
        while True:
            with store.lock(run_id):
                record = store.load(run_id)
                if record["state"] == "billing_stopped_confirmed":
                    return 8 if record.get("stop_reason") == "watchdog_cap" else 0
                refresh(store, record, driver, creds)
                if record.get("stop_reason") == "watchdog_cap" and record["marketplace_state"] == "absent":
                    code = down(store, record, driver, creds)
                    return 8 if code == 0 else code
                # Vast's API notes say "exited", "unknown" and "offline" never reach
                # "running". A new instance with no status yet is "starting", and the
                # bootstrap deadline below still destroys a run that never gets ready.
                # https://docs.vast.ai/api-reference/instances/show-instances
                if record["marketplace_state"] in ("exited", "unknown", "offline", "absent"):
                    code = down(store, record, driver, creds)
                    return code or 6
                if record.get("failure"):
                    if record["failure"]["reason"] == "speed_below_floor" and record.get("host_id"):
                        store.deny(record["host_id"], "speed_below_floor")
                    code = down(store, record, driver, creds)
                    return code or 6
                if local_stop_needed(record, time.time()):
                    record["stop_reason"] = "local_cap"
                    store.save(record)
                    code = down(store, record, driver, creds)
                    return code or 6
                if want_ready and not uploaded and record.get("ssh_host"):
                    ssh = SSH(store, record)
                    try:
                        # Wait for the SSH launch environment before copying the scripts.
                        ssh.run("command -v python3 && command -v bash && command -v curl")
                        ssh.upload(engines.bundle(entry_from_record(record), record))
                        key, watchdog = secret_get(run_id), secret_get("watchdog/" + run_id)
                        if not key or not watchdog:
                            raise CredentialError("A run secret is missing from the OS keyring. Run down.")
                        config = {"run_id": run_id, "model_id": record["model_id"],
                                  "instance_id": int(record["resource_ids"][0]),
                                  "started_at": record["started_at"], "hourly_rate": record["offer"]["hourly_all_in"],
                                  "transfer_price_per_gb": record["offer"]["bandwidth_cost_per_gb"],
                                  "cap_usd": record["cap_usd"], "advertised_mbps": record["offer"]["download_mbps"],
                                  "initial_cost_usd": record["offer"]["bandwidth_cost_per_gb"] * record["quote"]["request"]["image_gb"],
                                  "baseline_rx_bytes": 0}
                        ssh.start(config, key, watchdog)
                        uploaded = True
                    except RunError:
                        pass
                if want_ready and not record.get("ready"):
                    request = record["quote"]["request"]
                    allowed = (request["download_gb"] * 8000 / (record["offer"]["download_mbps"] * 3600)
                               + request["startup_hours"] + 10 / 60) * 3600
                    if time.time() - record["started_at"] > allowed:
                        code = down(store, record, driver, creds)
                        return code or 6
                current = record.get("stages", [])
                if current != stage:
                    print(f"{run_id}: stages {', '.join(current) or 'waiting for bootstrap'}", flush=True)
                    stage = current
                if want_ready and record.get("ready") and tunnel is None:
                    tunnel, port = SSH(store, record).open_tunnel(record["entry"]["profile"]["port"])
                    key = secret_get(run_id)
                    engines.health(f"http://127.0.0.1:{port}", key, record["model_id"])
                    record.update(state="running", local_port=port)
                    store.save(record)
                    endpoint(record)
                    if record.get("report_requested"):
                        from ablit.runs.store import atomic_json
                        atomic_json(store.root / "reports" / (run_id + ".json"),
                                    {"schema": 1, "model_id": record["model_id"], "image": record["image"],
                                     "stages": record.get("stages", []), "ready": True,
                                     "cap_usd": record["cap_usd"], "spend_estimate_usd": record["spend_estimate_usd"]})
                if not want_ready and tunnel is None:
                    tunnel, port = SSH(store, record).open_tunnel(record["entry"]["profile"]["port"])
                    record["local_port"] = port
                    store.save(record)
                    endpoint(record)
                if tunnel is not None and tunnel.poll() is not None:
                    raise RunError("The tunnel closed. Billing continues. Re-run tunnel or down.")
            # Each pass lists the account's instances, so poll slowly enough
            # to stay under Vast's rate limit.
            time.sleep(POLL_SECONDS)
    except KeyboardInterrupt:
        print(f"Billing continues. Run `ablit down {run_id}` to stop it.")
        return 0
    except (RunError, ValueError, httpx.HTTPError, subprocess.SubprocessError):
        with store.lock(run_id):
            code = down(store, store.load(run_id), driver, creds)
        return code or 6
    finally:
        if tunnel is not None:
            tunnel.terminate()
            tunnel.wait()


def entry_from_record(record):
    from ablit.catalog.models import Entry
    return Entry(**record["entry"])


def _up(args, entry, opts, driver, environ, store):
    if entry.id != "llama31-8b-abliterated-q4km" or args.endpoint != "tunnel":
        raise RunError("Step 3 supports the 8B llama.cpp entry on Vast through an SSH tunnel.")
    if args.backends and args.backends != "vast":
        raise RunError("Paid launches in step 3 support only --backends vast.")
    creds = credentials("vast", environ)
    driver.store = store
    with store.lock("selection"):
        active = store.active_model(entry.id)
        if active and not args.another:
            if len(active) == 1 and active[0]["state"] == "creating":
                record = active[0]
                if record["cap_usd"] != args.max_spend or record["entry"] != entry.model_dump():
                    raise RunError("The existing run has a different cap or build. Run down first.")
                with store.lock(record["run_id"]):
                    record = store.load(record["run_id"])
                    if record["state"] != "creating":
                        raise RunError("The run state changed. Check status before resuming.")
                    record = prepare(store, record, entry, driver, creds)
            else:
                raise RunError("An active run already exists for this model. Use tunnel, down, or --another.")
        else:
            lock = engines.engine_lock(entry)
            digest = lock.get("digest")
            import re
            if not isinstance(digest, str) or not re.fullmatch(r"sha256:[a-f0-9]{64}", digest):
                print("The engine image has no valid pinned digest.")
                return 5
            driver.denied_host_ids = store.denied_hosts()
            req = request_from_entry(entry, lock["image_gb"])
            report = run_plan(entry, req, opts, {"vast": driver}, wanted="vast", max_spend=args.max_spend,
                              environ={"VAST_API_KEY": creds.api_key}, strict_credentials=True)
            denied = store.denied_hosts()
            report = replace(report, ranked=[r for r in report.ranked if r.offer.host_id not in denied])
            print(render(report), end="")
            if not report.ranked:
                return 3
            offer = report.ranked[0].offer
            smallest = minimum_cap(offer, req, args.min_download or req.min_download_mbps)
            print(f"Smallest cap covering setup, ten minutes and the watchdog reserve: ${smallest:.2f}")
            if args.max_spend < smallest:
                return 5
            if args.catalog and not args.untested:
                raise RunError("A local catalog launch requires --untested.")
            engines.check_artifacts(entry)
            if input("Type rent to rent this machine: ").strip() != "rent":
                print("Rental cancelled.")
                return 2
            again = driver.requote(offer, creds)
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
                record["ssh_key_sent"] = True
                store.save(record)
                record["ssh_key_id"] = driver.register_ssh_key(public, creds)
                store.save(record)
                boot = BootSpec(record["image"], ONSTART, math.ceil(offer.allocated_disk_gb))
                try:
                    alloc = driver.create(offer, boot, record["run_id"], creds)
                except (httpx.HTTPError, CreateUncertain):
                    print(f"Create response is unknown. Re-run up or `ablit down {record['run_id']}` to recover by label.")
                    return 7
                record = store.load(record["run_id"])
                record["resource_ids"] = alloc.resource_ids
                store.save(record)
                record = prepare(store, record, entry, driver, creds)
    return monitor(store, record, driver, creds, want_ready=True)



def up(args, entry, opts, driver, environ, store):
    before = {r["run_id"]: r["state"] for r in store.all()}
    try:
        return _up(args, entry, opts, driver, environ, store)
    except (CredentialError, CredentialsRejected, BackendError, httpx.HTTPError,
            RunError, ValueError, subprocess.SubprocessError) as error:
        targets = [r for r in store.active_model(entry.id)
                   if r["run_id"] not in before or (before[r["run_id"]] == "creating" and r.get("resource_ids"))]
        if not targets:
            raise
        print(f"ablit: {type(error).__name__}: {error}", file=sys.stderr)
        creds = credentials("vast", environ)
        pending = False
        for record in targets:
            with store.lock(record["run_id"]):
                pending = down(store, store.load(record["run_id"]), driver, creds) == 7 or pending
        if pending:
            return 7
        if isinstance(error, (CredentialError, CredentialsRejected)):
            return 4
        if isinstance(error, WorkloadRejected):
            return 9
        return 6

def existing(args, driver, environ, store):
    records = store.all() if args.command == "status" and args.all else [store.select(args.run)]
    if not records:
        print(json.dumps({"schema": 1, "runs": []}) if getattr(args, "json", False) else "No runs.")
        return 0
    creds = credentials("vast", environ)
    if args.command == "status":
        while True:
            output, code = [], 0
            for selected in records:
                with store.lock(selected["run_id"]):
                    record = store.load(selected["run_id"])
                    redactor = run_redactor(record, creds)
                    def status_down():
                        with contextlib.redirect_stdout(sys.stderr):
                            return down(store, record, driver, creds)
                    if record["state"] != "billing_stopped_confirmed":
                        if record.get("create_sent") and not record.get("resource_ids"):
                            found = driver.reconcile(record["run_id"], creds)
                            record["resource_ids"] = found.resource_ids
                        refresh(store, record, driver, creds)
                        if args.watch and local_stop_needed(record, time.time()):
                            record["stop_reason"] = "local_cap"
                            store.save(record)
                            code = status_down() or 6
                        elif record["marketplace_state"] == "absent" and record.get("resource_ids"):
                            code = status_down()
                    if record["state"] in ("termination_requested", "resources_terminated"):
                        code = max(code, 7)
                    if record.get("stop_reason") == "watchdog_cap" and record["state"] == "billing_stopped_confirmed":
                        code = max(code, 8)
                    view = {k: v for k, v in record.items() if k not in ("entry", "quote", "offer")}
                    view["hourly_rate_usd"] = record["offer"]["hourly_all_in"]
                    view = json.loads(redactor(json.dumps(view)))
                    if args.show_key:
                        key = secret_get(record["run_id"])
                        if key is None:
                            raise CredentialError("The endpoint key is no longer in the keyring.")
                        view["endpoint_key"] = key
                    output.append(view)
            if args.json:
                print(json.dumps({"schema": 1, "runs": output}, indent=2), flush=True)
            else:
                for view in output:
                    print(json.dumps(view, indent=2), flush=True)
            if not args.watch or code or all(r["state"] == "billing_stopped_confirmed" for r in output):
                return code
            time.sleep(POLL_SECONDS)
    record = records[0]
    if args.command == "down":
        try:
            with store.lock(record["run_id"]):
                return down(store, store.load(record["run_id"]), driver, creds)
        except RunError:
            print("Cleanup is pending because another command holds the run lock. Re-run down.")
            return 7
    if args.command == "logs":
        SSH(store, record).logs(args.stage, args.follow, run_redactor(record, creds))
        return 0
    if record["state"] != "running":
        raise RunError("The endpoint is not running. Resume up for a creating run, or use down.")
    return monitor(store, record, driver, creds)
