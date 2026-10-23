"""Stream logs from the box over SSH."""

from __future__ import annotations

from ablit.runs.lifecycle import run_redactor
from ablit.runs.ssh import SSH


def logs(args, record, store, creds):
    SSH(store, record).logs(args.stage, args.follow, run_redactor(record, creds))
    return 0
