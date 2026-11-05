"""Re-open the SSH tunnel to a running endpoint."""

from __future__ import annotations

import sys

from ablit.runs import RunError
from ablit.runs.common import pair
from ablit.runs.monitor import monitor


def tunnel(record, store, drivers, environ):
    if record["state"] == "billing_stopped_confirmed":
        raise RunError("Billing has stopped for this run. Use `ablit up` to start a new rental.")
    if record["state"] in ("termination_requested", "resources_terminated"):
        print(f"Cleanup is pending. Run `ablit down {record['run_id']}` to confirm billing has stopped.",
              file=sys.stderr)
        return 7
    if record["state"] != "running":
        raise RunError("The run is still starting. Run the same `ablit up` command again with the same --max-spend to resume it, or run `ablit down`.")
    driver, creds = pair(record, drivers, environ)
    return monitor(store, record, driver, creds)
