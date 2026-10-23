"""Re-open the SSH tunnel to a running endpoint."""

from __future__ import annotations

from ablit.runs import RunError
from ablit.runs.monitor import monitor


def tunnel(record, store, driver, creds):
    if record["state"] != "running":
        raise RunError("The run is still starting. Run the same `ablit up` command again with the same --max-spend to resume it, or run `ablit down`.")
    return monitor(store, record, driver, creds)
