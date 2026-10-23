"""Dispatch the commands that act on an existing run."""

from __future__ import annotations

import json

from ablit.runs.common import pair
from ablit.runs.down import down_command
from ablit.runs.logs import logs
from ablit.runs.status import status
from ablit.runs.tunnel import tunnel


def existing(args, driver, environ, store, drivers=None):
    records = store.all() if args.command == "status" and args.all else [store.select(args.run)]
    if not records:
        print(json.dumps({"schema": 1, "runs": []}) if getattr(args, "json", False) else "No runs.")
        return 0
    drivers = drivers or {"vast": driver}
    driver, creds = pair(records[0], drivers, environ)
    if args.command == "status":
        return status(args, records, drivers, environ, store)
    record = records[0]
    if args.command == "down":
        return down_command(record, store, driver, creds)
    if args.command == "logs":
        return logs(args, record, store, creds)
    return tunnel(record, store, driver, creds)
