"""A machine that bills but never answers SSH must produce output and be destroyed on a clock."""

import json
import select
import signal
import subprocess
import sys
import time

import pytest

from ablit import credentials as keys
from ablit.backends import Creds
from ablit.runs import RunError, RunStore
from ablit.runs import monitor as monitor_mod
from ablit.runs import lifecycle
from ablit.runs import ssh as ssh_mod
from ablit.runs.ssh import SSH
from test_run_commands import saved_run

CREDS = Creds("invented-account-key")


def test_ssh_shutdown_kills_and_reaps_a_child_that_ignores_termination(monkeypatch):
    monkeypatch.setattr(ssh_mod, "SSH_STOP_TIMEOUT_SECONDS", 0.05)
    script = "import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); print('ready',flush=True); time.sleep(30)"
    proc = subprocess.Popen([sys.executable, "-c", script], stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    try:
        assert select.select([proc.stdout], [], [], 5)[0]
        assert proc.stdout.readline().strip() == "ready"
        ssh_mod.stop_ssh(proc)
        assert proc.returncode == -signal.SIGKILL
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=5)
        proc.stdout.close()


def test_ssh_shutdown_reports_a_process_that_still_will_not_exit():
    waits = []

    class Process:
        def terminate(self): pass
        def kill(self): pass
        def wait(self, timeout=None):
            waits.append(timeout)
            raise subprocess.TimeoutExpired("ssh", timeout)

    with pytest.raises(RunError, match="did not stop after it was killed"):
        ssh_mod.stop_ssh(Process())
    assert waits == [ssh_mod.SSH_STOP_TIMEOUT_SECONDS] * 2


def running_run(store, **changes):
    run = saved_run(store)
    run.update({"state": "creating", "host_id": "7301", "started_at": 1000.0, **changes})
    store.save(run)
    keys.secret_set("accounts/vast", "invented-account-key")
    return run


def patch_loop(monkeypatch, store, clock, ssh_class, downs):
    def refreshed(store_, record, driver, creds):
        record.update(marketplace_state="running", spend_estimate_usd=1.76, ssh_check="unavailable")
        store_.save(record)
    monkeypatch.setattr(monitor_mod, "refresh", refreshed)
    monkeypatch.setattr(monitor_mod, "SSH", ssh_class)
    monkeypatch.setattr(monitor_mod, "local_stop_needed", lambda record, now: False)
    monkeypatch.setattr(monitor_mod.time, "time", lambda: clock[0])
    monkeypatch.setattr(monitor_mod.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + monitor_mod.POLL_SECONDS))
    monkeypatch.setattr(monitor_mod, "down", lambda *args: downs.append(1) or 0)


class Fails:
    def __init__(self, *args): pass
    def run(self, command, *a, **kw): raise RunError("SSH failed.")


class TimesOut:
    """Stand-in for an SSH that always times out. The fixed SSH.run turns this into RunError."""
    def __init__(self, *args): pass
    def run(self, command, *a, **kw): raise RunError("SSH did not answer within 30 seconds.")


@pytest.mark.parametrize("ssh_class", [Fails, TimesOut])
def test_ssh_that_never_answers_prints_progress_then_denies_the_host_and_exits_6(monkeypatch, capsys, ssh_class):
    store = RunStore()
    run = running_run(store)
    clock, downs = [1000.0], []
    patch_loop(monkeypatch, store, clock, ssh_class, downs)
    assert monitor_mod.monitor(store, run, None, CREDS, want_ready=True) == 6
    out = capsys.readouterr().out
    assert out.count("waiting for SSH on the machine") >= monitor_mod.SSH_GRACE_SECONDS // monitor_mod.POLL_SECONDS
    assert "stages waiting for bootstrap" in out
    assert "SSH did not answer" in out and "Destroying the run" in out
    assert "7301" in store.denied_hosts()
    reasons = json.loads((store.root / "deny.json").read_text())["hosts"]["vast"]
    assert reasons["7301"] == "SSH did not answer after the machine was running"
    assert downs == [1]


def test_cleanup_not_confirmed_after_the_ssh_deadline_exits_7(monkeypatch):
    store = RunStore()
    run = running_run(store)
    clock = [1000.0]
    patch_loop(monkeypatch, store, clock, Fails, [])
    monkeypatch.setattr(monitor_mod, "down", lambda *args: 7)
    assert monitor_mod.monitor(store, run, None, CREDS, want_ready=True) == 7


def test_the_deadline_does_not_depend_on_ssh_raising(monkeypatch, capsys):
    """An SSH call that returns nothing useful and never raises must still hit the clock."""
    store = RunStore()
    run = running_run(store, ssh_host=None)  # no SSH host yet, so no SSH call is ever made
    clock, downs = [1000.0], []
    patch_loop(monkeypatch, store, clock, Fails, downs)
    assert monitor_mod.monitor(store, run, None, CREDS, want_ready=True) == 6
    assert downs == [1] and "waiting for SSH on the machine" in capsys.readouterr().out


def test_a_machine_that_answers_inside_the_grace_is_not_destroyed(monkeypatch):
    store = RunStore()
    run = running_run(store)
    clock, downs = [1000.0], []
    calls = []

    class Slow:
        def __init__(self, *args): pass
        def run(self, command, *a, **kw):
            calls.append(1)
            if len(calls) < 5:
                raise RunError("not yet")
            raise KeyboardInterrupt  # stands in for the upload starting
    patch_loop(monkeypatch, store, clock, Slow, downs)
    assert monitor_mod.monitor(store, run, None, CREDS, want_ready=True) == 7
    assert downs == [] and len(calls) == 5


def test_waiting_for_the_marketplace_prints_progress_each_pass(monkeypatch, capsys):
    store = RunStore()
    run = running_run(store, started_at=time.time())
    passes = []

    def refreshed(store_, record, driver, creds):
        passes.append(1)
        if len(passes) == 3:
            raise KeyboardInterrupt
        record.update(marketplace_state="loading", spend_estimate_usd=0.0)
        store_.save(record)
    monkeypatch.setattr(monitor_mod, "refresh", refreshed)
    monkeypatch.setattr(monitor_mod.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(monitor_mod, "local_stop_needed", lambda record, now: False)
    monitor_mod.monitor(store, run, None, CREDS, want_ready=True)
    assert capsys.readouterr().out.count("waiting for the marketplace to start the machine") == 2


def test_ssh_run_turns_a_timeout_into_run_error(monkeypatch):
    store = RunStore()
    run = running_run(store)
    ssh = SSH(store, run)
    monkeypatch.setattr(SSH, "args", lambda self: [sys.executable, "-c", "import time; time.sleep(60)"])
    started = time.monotonic()
    with pytest.raises(RunError, match="did not answer"):
        ssh.run("ignored", timeout=1)
    assert time.monotonic() - started < 10


def test_ssh_run_does_not_hang_when_a_child_keeps_the_pipes_open(monkeypatch):
    store = RunStore()
    run = running_run(store)
    ssh = SSH(store, run)
    # The shell starts a background sleep that inherits stdout, then waits. Killing only the shell
    # would leave the sleep holding the pipe, which is the known subprocess.run hang.
    script = "sleep 60 & wait"
    monkeypatch.setattr(SSH, "args", lambda self: ["sh", "-c", script, "x"])
    started = time.monotonic()
    with pytest.raises(RunError):
        ssh.run("ignored", timeout=1)
    assert time.monotonic() - started < 10


def test_ssh_run_reports_a_missing_ssh_program_as_run_error(monkeypatch):
    store = RunStore()
    run = running_run(store)
    monkeypatch.setattr(SSH, "args", lambda self: ["/nonexistent/ssh-for-test"])
    with pytest.raises(RunError, match="could not start"):
        SSH(store, run).run("ignored")


@pytest.mark.parametrize("follow", [False, True])
def test_reading_remote_logs_leaves_the_callers_piped_input_unread(tmp_path, follow):
    script = '''import sys
from pathlib import Path
from ablit.redact import Redactor
from ablit.runs import RunStore
from ablit.runs.ssh import SSH

ssh = SSH(RunStore(Path(sys.argv[1])), {"run_id": "test-run"})
ssh.args = lambda: [sys.executable, "-c", "import sys; sys.stdin.read(); print('log output')"]
ssh.logs(None, sys.argv[2] == "True", Redactor())
print(sys.stdin.read(), end="")
'''
    result = subprocess.run([sys.executable, "-c", script, str(tmp_path), str(follow)],
                            input="next input\n", capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert result.stdout == "log output\nnext input\n"


def test_refresh_treats_a_subprocess_timeout_from_the_probe_as_unavailable(monkeypatch):
    store = RunStore()
    run = running_run(store)

    class Driver:
        def observe(self, alloc, creds):
            from types import SimpleNamespace
            return SimpleNamespace(checked_at=1.0, state="running", allocation=None, hourly_rate=None)

    def probe(self):
        raise subprocess.TimeoutExpired("ssh", 30)
    monkeypatch.setattr(SSH, "probe", probe)
    record = lifecycle.refresh(store, run, Driver(), CREDS)
    assert record["ssh_check"] == "unavailable"
