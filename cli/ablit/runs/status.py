"""Show run state, and with --watch keep polling and stop a run that passed its cap."""

from __future__ import annotations

import contextlib
import json
import sys
import time

from ablit.credentials import CredentialError, secret_get
from ablit.runs import RunBusy
from ablit.runs.common import pair
from ablit.runs.lifecycle import down, local_stop_needed, refresh, run_redactor
from ablit.runs.monitor import POLL_SECONDS


def status(args, records, drivers, environ, store):
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
                driver, creds = pair(record, drivers, environ)
                redactor = run_redactor(record, creds)
                def status_down():
                    with contextlib.redirect_stdout(sys.stderr):
                        return down(store, record, driver, creds)  # noqa: B023 (called only within this iteration)
                if locked and record["state"] != "billing_stopped_confirmed":
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
