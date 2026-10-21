"""Status must not block a running `up`, the tunnel message must be true, and a machine that never starts is destroyed."""

import contextlib
import json
import time

import pytest
import respx

from ablit import credentials as keys
from ablit.backends import Creds
from ablit.cli import main
from ablit.runs import RunStore
from ablit.runs import commands
from test_run_commands import saved_run

CREDS = Creds("invented-account-key")


def prepared(monkeypatch, **changes):
    store = RunStore()
    run = saved_run(store)
    run.update(changes)
    store.save(run)
    keys.secret_set("accounts/vast", "invented-account-key")
    return store, run


def test_status_shows_the_saved_state_while_another_command_holds_the_lock(monkeypatch, capsys):
    store, run = prepared(monkeypatch)
    with respx.mock as router:  # any marketplace call would fail, because no route exists
        with store.lock(run["run_id"]):
            assert main(["status", run["run_id"], "--json"], environ={}) == 0
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data["runs"][0]["run_id"] == run["run_id"]
    assert "saved state" in captured.err
    assert len(router.calls) == 0


def test_monitor_skips_a_pass_when_the_lock_is_busy_and_does_not_stop_the_run(monkeypatch, capsys):
    store, run = prepared(monkeypatch, marketplace_state="running")
    monkeypatch.setattr(commands, "MONITOR_LOCK_WAIT_SECONDS", 0)
    held = contextlib.ExitStack()
    held.enter_context(store.lock(run["run_id"]))
    refreshed = []

    def refresh(store_, record, driver, creds):
        refreshed.append(True)
        raise KeyboardInterrupt

    monkeypatch.setattr(commands, "refresh", refresh)
    monkeypatch.setattr(commands.time, "sleep", lambda seconds: held.close())
    monkeypatch.setattr(commands, "down", lambda *a: pytest.fail("a busy lock must not destroy the run"))
    assert commands.monitor(store, run, None, CREDS) == 0
    out = capsys.readouterr().out
    assert "Skipping this check" in out and "stopping the run" not in out
    assert refreshed == [True]


def test_a_machine_that_never_reaches_running_is_destroyed_with_exit_6(monkeypatch, capsys):
    store, run = prepared(monkeypatch, started_at=time.time() - commands.START_DEADLINE_SECONDS - 60)

    def refresh(store_, record, driver, creds):
        record.update(marketplace_state="loading", spend_estimate_usd=0.01)
        store_.save(record)

    destroyed = []
    monkeypatch.setattr(commands, "refresh", refresh)
    monkeypatch.setattr(commands, "down", lambda *a: destroyed.append(True) or 0)
    assert commands.monitor(store, run, None, CREDS, want_ready=True) == 6
    assert destroyed == [True]
    assert "the marketplace did not start the machine in time" in capsys.readouterr().out


def test_a_machine_still_loading_inside_the_deadline_is_left_alone(monkeypatch):
    store, run = prepared(monkeypatch, started_at=time.time() - 300)

    def refresh(store_, record, driver, creds):
        record.update(marketplace_state="loading", spend_estimate_usd=0.01)
        store_.save(record)

    monkeypatch.setattr(commands, "refresh", refresh)
    monkeypatch.setattr(commands, "down", lambda *a: pytest.fail("inside the deadline"))
    monkeypatch.setattr(commands.time, "sleep", lambda seconds: (_ for _ in ()).throw(KeyboardInterrupt))
    assert commands.monitor(store, run, None, CREDS, want_ready=True) == 0


def test_tunnel_on_a_creating_run_says_how_to_resume(monkeypatch, capsys):
    prepared(monkeypatch, state="creating")
    with respx.mock:
        assert main(["tunnel"], environ={}) == 2
    err = capsys.readouterr().err
    assert "The run is still starting. Run the same `ablit up` command again with the same --max-spend" in err
    assert "Resume up" not in err
