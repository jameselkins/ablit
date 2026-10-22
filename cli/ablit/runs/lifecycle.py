"""Reconcile resources, save watchdog evidence and retry incomplete cleanup."""

from __future__ import annotations

import json
import math
import time

import httpx

from ablit.backends.base import Allocation, Inventory
from ablit.backends.errors import BackendError
from ablit.credentials import CredentialError, secret_delete, secret_get
from ablit.redact import Redactor
from ablit.runs import RunError
from ablit.runs.ssh import SSH
from ablit.watchdog.ablit_watchdog import Config, reserve

CONSOLE = "https://cloud.vast.ai/instances/"
KEYS = "https://cloud.vast.ai/manage-keys/"
CONSOLES = {"vast": CONSOLE, "runpod": "https://console.runpod.io/pods", "clore": "https://clore.ai/"}
DISPLAY = {"vast": "Vast", "runpod": "RunPod", "clore": "Clore"}
LISTS = {"vast": "Instances are still in the user's instance list.",
         "runpod": "Pods are still in the user's pod list.",
         "clore": "Orders are still in the user's order list."}
UNRECOVERED = {"vast": "Create was sent but no ID has been recovered. Check the run label in the console.",
               "clore": "Create was sent but no order ID has been recovered. Clore orders carry no label, so check "
                        "My Orders in the Clore console for an order on the rented server."}


def reason(error: Exception) -> str:
    """Name the failure without echoing a response body."""
    text = str(error).splitlines()[0][:160] if str(error) else ""
    return f"{type(error).__name__}: {text}" if text else type(error).__name__


def allocation(record: dict) -> Allocation:
    return Allocation(record["run_id"], record.get("backend", "vast"), record.get("resource_ids", []),
                      record["offer"]["hourly_all_in"], record.get("ssh_host"), record.get("ssh_port"),
                      record.get("host_id"))


def run_redactor(record: dict, creds) -> Redactor:
    secrets = [creds.api_key]
    for name in (record["run_id"], "watchdog/" + record["run_id"]):
        try:
            secrets.append(secret_get(name))
        except CredentialError:
            pass
    return Redactor(s for s in secrets if s and len(s) >= 6)


PROGRESS_STEPS = ("download", "join", "extract")
PROGRESS_FIELDS = ("bytes_done", "bytes_total", "mbps", "files_done", "files_total", "at")


def clean_progress(value) -> dict:
    """Keep only the known steps and numeric fields of the remote progress files.

    A malformed file is dropped, because progress is for display and must never
    stop a run.
    """
    if not isinstance(value, dict):
        return {}
    clean = {}
    for name in PROGRESS_STEPS:
        item = value.get(name)
        if not isinstance(item, dict):
            continue
        row = {k: item[k] for k in PROGRESS_FIELDS
               if type(item.get(k)) in (int, float) and math.isfinite(item[k]) and item[k] >= 0}
        if "bytes_done" in row and "bytes_total" in row:
            clean[name] = row
    return clean


def refresh(store, record: dict, driver, creds, *, remote=True) -> dict:
    """Read marketplace state and copy only known non-secret remote fields."""
    observation = driver.observe(allocation(record), creds)
    record["last_marketplace_check"] = observation.checked_at
    record["marketplace_state"] = observation.state
    if observation.allocation:
        alloc = observation.allocation
        record.update(ssh_host=alloc.ssh_host, ssh_port=alloc.ssh_port, host_id=alloc.host_id)
    if observation.hourly_rate is not None:
        record["observed_hourly_rate"] = observation.hourly_rate
    if remote and record.get("ssh_host") and observation.state != "absent":
        try:
            probe = SSH(store, record).probe()
            heartbeat = probe.get("heartbeat")
            if heartbeat:
                at, spent = heartbeat.get("at"), heartbeat.get("spend")
                if (type(at) not in (int, float) or type(spent) not in (int, float)
                        or not math.isfinite(at) or not math.isfinite(spent) or spent < 0
                        or heartbeat.get("action") not in ("continue", "destroy_cap", "destroy_stall")):
                    raise RunError("The remote heartbeat has an unknown shape.")
                if heartbeat.get("rx_bytes") is not None and type(heartbeat["rx_bytes"]) is not int:
                    raise RunError("The remote heartbeat has an unknown shape.")
                record["heartbeat"] = {k: heartbeat[k] for k in ("at", "spend", "action", "rx_bytes") if k in heartbeat}
                if heartbeat.get("action") == "destroy_cap":
                    record["stop_reason"] = "watchdog_cap"
            stages = probe.get("stages", [])
            expected = {"01-start.done", "02-check.done", "03-randread.done", "04-speed.done", "05-download.done",
                        "06-staging.done", "07-engine.done", "08-health.done", "09-ready.done"}
            if not isinstance(stages, list) or any(s not in expected for s in stages):
                raise RunError("The stage list contains unknown markers.")
            record["stages"] = stages
            record["progress"] = clean_progress(probe.get("progress"))
            if "failed" in probe:
                failed = probe["failed"]
                reason = str(failed.get("reason", "unknown"))
                record["failure"] = {"reason": "speed_below_floor" if reason == "speed_below_floor" else "bootstrap_failed",
                                     "detail": run_redactor(record, creds)(f"{reason}: {failed.get('detail', '')}")[:500]}
            if "ready" in probe:
                record["ready"] = True
        except (RunError, OSError, ValueError, httpx.HTTPError):
            record["ssh_check"] = "unavailable"
    rate = max(record["offer"]["hourly_all_in"], record.get("observed_hourly_rate", 0))
    elapsed = max(time.time() - record["started_at"], 0) / 3600
    # The API documents the rate but no cumulative transfer charge. Count the
    # quoted image and model transfer locally, and take the larger heartbeat total.
    estimated = rate * elapsed + record.get("transfer_estimate_usd", 0) + record["offer"].get("fixed_fees", 0)
    record["spend_estimate_usd"] = max(estimated, record.get("heartbeat", {}).get("spend", 0))
    store.save(record)
    return record


def local_stop_needed(record: dict, now: float) -> bool:
    """Use the local check if the on-box heartbeat is over three minutes old."""
    if record["marketplace_state"] == "absent":
        return False
    last = record.get("heartbeat", {}).get("at", record["started_at"])
    # A backend with no scoped on-box key (RunPod, Clore) cannot rely on the watchdog to stop the run,
    # so the local check runs whatever the heartbeat age is.
    stale = now - last > 180 or bool(record.get("local_cap_primary"))
    cfg = Config(record["started_at"], record["offer"]["hourly_all_in"],
                 record["offer"]["bandwidth_cost_per_gb"], record["cap_usd"],
                 record["offer"]["download_mbps"] or record.get("min_download_mbps", 0))
    return stale and record["spend_estimate_usd"] + reserve(cfg) >= record["cap_usd"]


def down(store, record: dict, driver, creds) -> int:
    """Keep all evidence until resources, billing and credential cleanup are confirmed."""
    run_id = record["run_id"]
    if record["state"] == "billing_stopped_confirmed":
        print(f"{run_id}: billing_stopped_confirmed. Billing stopped.")
        return 0
    record["state"] = "termination_requested"
    record["pending_cleanup"] = []
    store.save(record)
    errors = []
    billing_clear = False
    try:
        found = driver.reconcile(run_id, creds)
        record["resource_ids"] = sorted(set(record.get("resource_ids", [])) | set(found.resource_ids))
        store.save(record)
    except (BackendError, httpx.HTTPError) as error:
        errors.append(f"Instance reconciliation is unconfirmed ({reason(error)}).")
    backend = record.get("backend", "vast")
    inventory = Inventory(run_id, backend, record.get("resource_ids", []))
    for resource in inventory.resource_ids:
        try:
            driver.destroy(Inventory(run_id, backend, [resource]), creds)
        except (BackendError, httpx.HTTPError) as error:
            errors.append(f"Destroy of instance {resource} is unconfirmed ({reason(error)}).")
    if not errors:
        record["state"] = "resources_terminated"
        store.save(record)
    try:
        evidence = driver.verify_billing_stopped(inventory, creds)
        record["last_marketplace_check"] = evidence.checked_at
        record["still_billable"] = evidence.still_billable
        if evidence.still_billable:
            errors.append(LISTS.get(backend, LISTS["runpod"]))
        elif not record.get("resource_ids") and record.get("create_sent") and not record.get("create_rejected"):
            # An empty list after an ambiguous create does not prove that an
            # accepted create cannot appear later. Vast documents no upper bound.
            errors.append(UNRECOVERED.get(backend) or
                          f"Create was sent but no ID has been recovered. Check for a pod named ablit-{run_id} in the console.")
        else:
            record["state"] = "resources_terminated"
            billing_clear = True
        if evidence.final_cost_usd is not None:
            record["final_cost_usd"] = evidence.final_cost_usd
    except (BackendError, httpx.HTTPError) as error:
        errors.append(f"Billing stop is unconfirmed ({reason(error)}).")
    # Keep the watchdog usable until the marketplace confirms all instances absent.
    if billing_clear:
        if record.get("watchdog_key_id"):
            try:
                driver.delete_watchdog_key(record["watchdog_key_id"], creds)
                record["watchdog_key_deleted"] = True
                store.save(record)
            except (BackendError, httpx.HTTPError):
                errors.append("Watchdog key deletion is unconfirmed.")
        elif record.get("watchdog_key_sent"):
            errors.append(f"The watchdog key response was lost. Review and revoke it at {KEYS}")
        if record.get("ssh_key_id") and not record.get("ssh_key_deleted"):
            try:
                driver.delete_ssh_key(record["ssh_key_id"], creds)
                record["ssh_key_deleted"] = True
                store.save(record)
            except (BackendError, httpx.HTTPError):
                errors.append("SSH key deletion is unconfirmed.")
        elif record.get("ssh_key_sent") and not record.get("ssh_key_id"):
            errors.append("SSH key registration was unconfirmed. Review the public key in the Vast console.")
    if not errors:
        try:
            secret_delete("watchdog/" + run_id)
            secret_delete(run_id)
        except CredentialError:
            errors.append("Local keyring cleanup is pending.")
    record["pending_cleanup"] = errors
    if not errors:
        record["state"] = "billing_stopped_confirmed"
    store.save(record)
    if errors:
        print(f"{run_id}: {record['state']}. Cleanup pending. Check {CONSOLES.get(backend, CONSOLE)}")
        for error in errors:
            print(error)
        return 7
    print(f"{run_id}: billing_stopped_confirmed. Billing stopped.")
    if "final_cost_usd" in record:
        print(f"Final cost reported by {DISPLAY.get(backend, backend)}: ${record['final_cost_usd']:.4f}")
    return 0


def save_remote_logs(store, record: dict, creds) -> None:
    """Copy the end of the box's logs before a failed run is destroyed.

    It never raises, because cleanup must go ahead even when SSH is down.
    """
    try:
        text = SSH(store, record).run("tail -n 300 /ablit/logs/*.log 2>/dev/null", timeout=20)
        path = store.root / "logs" / f"{record['run_id']}.log"
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.write_text(run_redactor(record, creds)(text.decode("utf-8", "replace")), encoding="utf-8")
        path.chmod(0o600)
        print(f"{record['run_id']}: saved the machine's logs to {path}", flush=True)
    except Exception:
        pass
