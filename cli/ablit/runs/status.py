"""Show run state, and with --watch keep polling and stop a run that passed its cap."""

from __future__ import annotations

import contextlib
import json
import sys
import time

import httpx

from ablit.backends.errors import BackendError, CredentialsRejected, WorkloadRejected
from ablit.credentials import CredentialError, secret_get
from ablit.runs import RunBusy
from ablit.runs.common import pair
from ablit.runs.lifecycle import down, estimate_spend, local_stop_needed, reason, refresh, run_redactor
from ablit.runs.monitor import POLL_SECONDS


def key_only(record) -> int:
    """Print the run's endpoint key alone, so a shell can capture it with $(...)."""
    key = secret_get(record["run_id"])
    if key is None:
        raise CredentialError("The endpoint key is no longer in the keyring.")
    print(key, flush=True)
    return 0


def status(args, records, drivers, environ, store):
    watching_active_runs = args.watch and args.all and any(
        record["state"] != "billing_stopped_confirmed" for record in records
    )
    while True:
        output, code = [], 0
        for selected in records:
            with contextlib.ExitStack() as held:
                # Status never blocks a running `up`. When the lock is busy it shows the saved record.
                try:
                    held.enter_context(store.lock(selected["run_id"]))
                    locked = True
                except RunBusy:
                    locked = False
                    print(f"{selected['run_id']}: another command holds the run lock, so this is the saved state.",
                          file=sys.stderr)
                record = store.load(selected["run_id"])
                billing_stopped = record["state"] == "billing_stopped_confirmed"
                try:
                    driver, creds = pair(record, drivers, environ)
                except CredentialError:
                    if locked and not billing_stopped:
                        raise
                    driver, creds = None, None
                redactor = run_redactor(record, creds)
                def status_down():
                    with contextlib.redirect_stdout(sys.stderr):
                        return down(store, record, driver, creds)  # noqa: B023 (called only within this iteration)
                if locked and not billing_stopped:
                    marketplace_unavailable = False
                    try:
                        if record.get("create_sent") and not record.get("resource_ids"):
                            found = driver.reconcile(record["run_id"], creds)
                            record["resource_ids"] = found.resource_ids
                        refresh(store, record, driver, creds)
                    except (BackendError, httpx.HTTPError) as error:
                        if not args.watch or isinstance(error, (CredentialsRejected, WorkloadRejected)):
                            raise
                        marketplace_unavailable = True
                        record["marketplace_check"] = "unavailable"
                        record["spend_estimate_usd"] = max(
                            record.get("spend_estimate_usd", 0), estimate_spend(record, time.time()),
                        )
                        store.save(record)
                        print(f"{record['run_id']}: marketplace check failed ({reason(error, redactor)}). Retrying.",
                              file=sys.stderr, flush=True)
                    else:
                        if record.pop("marketplace_check", None) is not None:
                            store.save(record)
                    # An earlier absence is not a fresh billing confirmation during an outage.
                    cap_record = {**record, "marketplace_state": "unknown"} if marketplace_unavailable else record
                    if args.watch and local_stop_needed(cap_record, time.time()):
                        record["stop_reason"] = record.get("stop_reason") or "local_cap"
                        store.save(record)
                        code = max(code, status_down() or 6)
                    elif (not marketplace_unavailable and record["marketplace_state"] == "absent"
                          and record.get("resource_ids")):
                        code = max(code, status_down())
                if record["state"] in ("termination_requested", "resources_terminated"):
                    code = max(code, 7)
                historical_stop = watching_active_runs and selected["state"] == "billing_stopped_confirmed"
                if (not historical_stop and record.get("stop_reason") == "watchdog_cap"
                        and record["state"] == "billing_stopped_confirmed"):
                    code = max(code, 8)
                view = {k: v for k, v in record.items() if k not in ("entry", "quote", "offer", "local_tunnel_id")}
                view["hourly_rate_usd"] = record["offer"]["hourly_all_in"]
                try:
                    view["saved_log_available"] = store.saved_log_path(record["run_id"]).is_file()
                except OSError:
                    view["saved_log_available"] = False
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
