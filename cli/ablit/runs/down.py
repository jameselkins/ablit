"""Destroy every billable resource a run created."""

from __future__ import annotations

from ablit.runs import RunBusy
from ablit.runs.common import pair
from ablit.runs.lifecycle import down

# An attached `up` holds the run lock for one poll, which SSH timeouts can stretch.
LOCK_WAIT_SECONDS = 90


def down_command(run_id, store, drivers, environ):
    try:
        with store.lock(run_id, wait=LOCK_WAIT_SECONDS):
            record = store.load(run_id)
            if record["state"] == "billing_stopped_confirmed":
                return down(store, record, None, None)
            driver, creds = pair(record, drivers, environ)
            return down(store, record, driver, creds)
    except RunBusy:
        print("Cleanup is pending because another command holds the run lock. Re-run down.")
        return 7
