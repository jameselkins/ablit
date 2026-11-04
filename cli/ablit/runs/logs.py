"""Stream logs from the machine or read a saved copy."""

from __future__ import annotations

from ablit.redact import strip_control
from ablit.runs import RunError
from ablit.runs.lifecycle import run_redactor
from ablit.runs.ssh import SSH


def logs(args, record, store, creds):
    SSH(store, record).logs(args.stage, args.follow, run_redactor(record, creds))
    return 0


def saved_logs(args, record, store):
    if args.stage or args.follow:
        raise RunError("--saved cannot be combined with --stage or -f.")
    run_id = record["run_id"]
    try:
        text = store.saved_log_path(run_id).read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError as error:
        raise RunError(f"No saved log for run {run_id}.") from error
    except OSError as error:
        raise RunError(f"Cannot read the saved log for run {run_id}.") from error
    print(strip_control(text), end="")
    return 0
