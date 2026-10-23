"""Destroy every billable resource a run created."""

from __future__ import annotations

from ablit.runs import RunError
from ablit.runs.lifecycle import down

# An attached `up` holds the run lock for one poll, which SSH timeouts can stretch.
LOCK_WAIT_SECONDS = 90


def down_command(record, store, driver, creds):
    try:
        with store.lock(record["run_id"], wait=LOCK_WAIT_SECONDS):
            return down(store, store.load(record["run_id"]), driver, creds)
    except RunError:
        print("Cleanup is pending because another command holds the run lock. Re-run down.")
        return 7
