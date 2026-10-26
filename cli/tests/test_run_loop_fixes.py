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
from ablit.runs import monitor as monitor_mod
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
    monkeypatch.setattr(monitor_mod, "MONITOR_LOCK_WAIT_SECONDS", 0)
    held = contextlib.ExitStack()
    held.enter_context(store.lock(run["run_id"]))
    refreshed = []

    def refresh(store_, record, driver, creds):
        refreshed.append(True)
        raise KeyboardInterrupt

    monkeypatch.setattr(monitor_mod, "refresh", refresh)
    monkeypatch.setattr(monitor_mod.time, "sleep", lambda seconds: held.close())
    monkeypatch.setattr(monitor_mod, "down", lambda *a: pytest.fail("a busy lock must not destroy the run"))
    assert monitor_mod.monitor(store, run, None, CREDS) == 0
    out = capsys.readouterr().out
    assert "Skipping this check" in out and "stopping the run" not in out
    assert refreshed == [True]


def test_a_machine_that_never_reaches_running_is_destroyed_with_exit_6(monkeypatch, capsys):
    store, run = prepared(monkeypatch, started_at=time.time() - monitor_mod.START_DEADLINE_SECONDS - 60)

    def refresh(store_, record, driver, creds):
        record.update(marketplace_state="loading", spend_estimate_usd=0.01)
        store_.save(record)

    destroyed = []
    monkeypatch.setattr(monitor_mod, "refresh", refresh)
    monkeypatch.setattr(monitor_mod, "down", lambda *a: destroyed.append(True) or 0)
    assert monitor_mod.monitor(store, run, None, CREDS, want_ready=True) == 6
    assert destroyed == [True]
    assert "the marketplace did not start the machine in time" in capsys.readouterr().out


def test_a_machine_still_loading_inside_the_deadline_is_left_alone(monkeypatch):
    store, run = prepared(monkeypatch, started_at=time.time() - 300)

    def refresh(store_, record, driver, creds):
        record.update(marketplace_state="loading", spend_estimate_usd=0.01)
        store_.save(record)

    monkeypatch.setattr(monitor_mod, "refresh", refresh)
    monkeypatch.setattr(monitor_mod, "down", lambda *a: pytest.fail("inside the deadline"))
    monkeypatch.setattr(monitor_mod.time, "sleep", lambda seconds: (_ for _ in ()).throw(KeyboardInterrupt))
    assert monitor_mod.monitor(store, run, None, CREDS, want_ready=True) == 7


def test_tunnel_on_a_creating_run_says_how_to_resume(monkeypatch, capsys):
    prepared(monkeypatch, state="creating")
    with respx.mock:
        assert main(["tunnel"], environ={}) == 2
    err = capsys.readouterr().err
    assert "The run is still starting. Run the same `ablit up` command again with the same --max-spend" in err
    assert "Resume up" not in err


class StubDriver:
    """A driver whose cleanup works, or fails when `fail` is set."""

    def __init__(self, fail=False):
        self.fail = fail

    def reconcile(self, run_id, creds):
        from types import SimpleNamespace
        return SimpleNamespace(resource_ids=["7201"])

    def destroy(self, inventory, creds):
        if self.fail:
            from ablit.backends.errors import RequestRejected
            raise RequestRejected("vast", 500, "down")

    def verify_billing_stopped(self, inventory, creds):
        from ablit.backends.base import BillingEvidence
        return BillingEvidence(time.time(), [])


def never_starts(monkeypatch):
    store, run = prepared(monkeypatch, started_at=time.time() - monitor_mod.START_DEADLINE_SECONDS - 60,
                          watchdog_key_id=None, watchdog_key_sent=False)

    def refresh(store_, record, driver, creds):
        record.update(marketplace_state="starting", spend_estimate_usd=0.01)
        store_.save(record)

    monkeypatch.setattr(monitor_mod, "refresh", refresh)
    return store, run


def test_start_deadline_with_confirmed_cleanup_exits_6(monkeypatch):
    store, run = never_starts(monkeypatch)
    assert monitor_mod.monitor(store, run, StubDriver(), CREDS, want_ready=True) == 6
    assert store.load(run["run_id"])["state"] == "billing_stopped_confirmed"


def test_start_deadline_with_unconfirmed_cleanup_exits_7(monkeypatch):
    store, run = never_starts(monkeypatch)
    assert monitor_mod.monitor(store, run, StubDriver(fail=True), CREDS, want_ready=True) == 7


def test_a_run_stopped_by_another_command_before_ready_exits_6(monkeypatch, capsys):
    store, run = prepared(monkeypatch, state="billing_stopped_confirmed")
    monkeypatch.setattr(monitor_mod, "refresh", lambda *a: pytest.fail("the run is already stopped"))
    assert monitor_mod.monitor(store, run, None, CREDS, want_ready=True) == 6
    assert "stopped before the engine was ready" in capsys.readouterr().out


def test_a_stopped_run_is_still_a_clean_exit_for_tunnel_and_status(monkeypatch):
    store, run = prepared(monkeypatch, state="billing_stopped_confirmed")
    assert monitor_mod.monitor(store, run, None, CREDS) == 0


def test_ctrl_c_before_the_endpoint_is_ready_does_not_exit_0(monkeypatch):
    store, run = prepared(monkeypatch, started_at=time.time(), marketplace_state="starting")
    monkeypatch.setattr(monitor_mod, "refresh", lambda *a: (_ for _ in ()).throw(KeyboardInterrupt))
    assert monitor_mod.monitor(store, run, None, CREDS, want_ready=True) == 7


def test_a_keyring_error_during_the_monitor_destroys_the_run(monkeypatch, capsys):
    store, run = prepared(monkeypatch, marketplace_state="running")

    def refresh(store_, record, driver, creds):
        raise keys.CredentialError("The OS keyring is unavailable.")

    destroyed = []
    monkeypatch.setattr(monitor_mod, "refresh", refresh)
    monkeypatch.setattr(monitor_mod, "save_remote_logs", lambda *a: None)
    monkeypatch.setattr(monitor_mod, "down", lambda *a: destroyed.append(True) or 0)
    assert monitor_mod.monitor(store, run, None, CREDS, want_ready=True) == 6
    assert destroyed == [True]
    assert "stopping the run because of CredentialError" in capsys.readouterr().out


def test_an_unexpected_error_with_an_active_run_exits_7(monkeypatch, capsys):
    prepared(monkeypatch)

    def broken(*args, **kwargs):
        raise TypeError("invented bug")

    monkeypatch.setattr("ablit.cli._main", broken)
    assert main(["status"], environ={}) == 7
    err = capsys.readouterr().err
    assert "TypeError: invented bug" in err and "may still be billing" in err


def test_an_unexpected_error_with_no_active_run_exits_1(monkeypatch, capsys):
    def broken(*args, **kwargs):
        raise TypeError("invented bug")

    monkeypatch.setattr("ablit.cli._main", broken)
    assert main(["status"], environ={}) == 1
    assert "may still be billing" not in capsys.readouterr().err
