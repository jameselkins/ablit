"""Failure cases from the repository review, with no paid resources or account access."""

import contextlib
import io
import json
import shutil
import tarfile
from dataclasses import replace

import httpx
import pytest
import respx

from ablit import credentials as keys, engines
from ablit.backends.base import Creds, OfferSearch
from ablit.backends.errors import CredentialsRejected, ResponseNotRecognized
from ablit.catalog import load_catalog, resolve
from ablit.cli import main
from ablit.plan import report_json, run_plan
from ablit.runs import RunBusy, RunStore
from ablit.runs import monitor as monitor_mod
from ablit.runs import status as status_mod, up as up_mod
from ablit.runs.lifecycle import local_stop_needed
from ablit.runs.store import atomic_json
from ablit.select import PlanOptions
from ablit.select.request import request_from_entry
from helpers import make_offer
from test_run_commands import MODEL, launch_environment, launch_routes, saved_run

LIUM_HOST = "11111111-1111-4111-8111-111111111111"


@pytest.mark.parametrize("action", ["continue", "destroy_cap"])
def test_local_cap_is_enforced_despite_a_fresh_watchdog_heartbeat(action):
    run = saved_run(RunStore())
    run.update(marketplace_state="running", spend_estimate_usd=run["cap_usd"], local_cap_primary=False,
               heartbeat={"at": 1000, "spend": run["cap_usd"], "action": action})
    assert local_stop_needed(run, 1000)


@pytest.mark.parametrize("cleanup_code", [0, 7])
def test_monitor_takes_over_when_the_watchdog_cannot_destroy(monkeypatch, cleanup_code):
    store = RunStore()
    run = saved_run(store)
    stops = []

    def refresh(store_, record, driver, creds):
        record.update(marketplace_state="running", spend_estimate_usd=record["cap_usd"],
                      stop_reason="watchdog_cap", heartbeat={"at": 1000, "action": "destroy_cap"})

    monkeypatch.setattr(monitor_mod, "refresh", refresh)
    monkeypatch.setattr(monitor_mod.time, "time", lambda: 1000)
    monkeypatch.setattr(monitor_mod.time, "sleep", lambda seconds: pytest.fail("the cap must stop the first poll"))
    monkeypatch.setattr(monitor_mod.SSH, "run", lambda *a, **kw: pytest.fail("stop before SSH"))
    monkeypatch.setattr(monitor_mod, "down", lambda *a: stops.append(True) or cleanup_code)
    assert monitor_mod.monitor(store, run, None, Creds("invented-account-key"), want_ready=True) == (cleanup_code or 8)
    assert stops == [True]


def test_bundle_uses_the_installed_package_layout(repo_root, tmp_path, monkeypatch):
    entry = resolve(load_catalog().entries, MODEL)
    record = saved_run(RunStore())
    data = tmp_path / "_data"
    shutil.copytree(repo_root / "engines", data / "engines")
    monkeypatch.setattr(engines, "repo_root", lambda: data)
    with tarfile.open(fileobj=io.BytesIO(engines.bundle(entry, record))) as archive:
        watchdog = archive.extractfile("ablit_watchdog.py").read()
    compile(watchdog, "ablit_watchdog.py", "exec")


def test_a_bundle_failure_is_found_before_any_resource_is_created(monkeypatch):
    launch_environment(monkeypatch)
    monkeypatch.setattr("builtins.input", lambda prompt: "rent")

    def broken_bundle(*args):
        raise FileNotFoundError("invented missing bootstrap file")

    monkeypatch.setattr(engines, "bundle", broken_bundle)
    monkeypatch.setattr(monitor_mod.SSH, "run", lambda *a, **kw: b"")
    with respx.mock(assert_all_called=False) as router:
        create, scoped = launch_routes(router)
        assert main(["up", MODEL, "--max-spend", "5"], environ={}) == 1
        assert create.call_count == scoped.call_count == 0
        assert not any(call.request.url.path == "/api/v0/ssh/" for call in router.calls)
    assert RunStore().all() == []


def test_denied_hosts_are_separate_for_each_marketplace(tmp_path):
    store = RunStore(tmp_path)
    store.deny("7301", "speed_below_floor", backend="clore")
    store.deny(LIUM_HOST, "SSH did not answer", backend="lium")
    store.deny("7301", "SSH did not answer", backend="vast")
    reopened = RunStore(tmp_path)
    assert reopened.denied_hosts("vast") == {"7301"}
    assert reopened.denied_hosts("clore") == {"7301"}
    assert reopened.denied_hosts("lium") == {LIUM_HOST}
    assert reopened.denied_hosts("runpod") == set()


def test_a_denied_lium_host_does_not_break_a_vast_launch(monkeypatch):
    launch_environment(monkeypatch)
    store = RunStore()
    store.deny(LIUM_HOST, "SSH did not answer", backend="lium")
    monkeypatch.setattr("builtins.input", lambda prompt: "cancel")
    with respx.mock(assert_all_called=False) as router:
        launch_routes(router)
        assert main(["up", MODEL, "--max-spend", "5"], environ={}) == 2


@pytest.mark.parametrize("host,backend", [("7301", "vast"), (LIUM_HOST, "lium")])
def test_legacy_denials_without_run_records_are_preserved(tmp_path, host, backend):
    store = RunStore(tmp_path)
    atomic_json(tmp_path / "deny.json", {"hosts": {host: "speed_below_floor"}})
    assert store.denied_hosts(backend) == {host}
    assert store.denied_hosts("clore") == set()
    store.deny("other", "SSH did not answer", backend=backend)
    assert RunStore(tmp_path).denied_hosts(backend) == {host, "other"}


def test_legacy_denials_use_the_marketplace_in_saved_run_records(tmp_path):
    store = RunStore(tmp_path)
    run = saved_run(store)
    run.update(backend="clore", host_id="7301")
    store.save(run)
    atomic_json(tmp_path / "deny.json", {"hosts": {"7301": "speed_below_floor"}})
    assert store.denied_hosts("clore") == {"7301"}
    assert store.denied_hosts("vast") == set()


@pytest.mark.parametrize("error", [httpx.ReadTimeout("invented timeout"), httpx.ConnectError("invented outage"),
                                  CredentialsRejected("vast", 401, "invented refusal"),
                                  ResponseNotRecognized("invented shape")])
def test_failed_marketplace_searches_make_the_overall_report_incomplete(error):
    entry = resolve(load_catalog().entries, MODEL)

    class Broken:
        def search(self, *args):
            raise error

    class Working:
        def search(self, *args):
            return OfferSearch("runpod", [make_offer(backend="runpod")], complete=True)

    report = run_plan(entry, request_from_entry(entry), PlanOptions(),
                      {"vast": Broken(), "runpod": Working()}, environ={})
    data = report_json(report)
    assert data["chosen"]["backend"] == "runpod"
    assert data["complete"] is False
    assert data["incomplete_label"] == "cheapest of the 1 offers checked"


def test_another_command_taking_the_lock_after_release_cannot_stop_the_monitor(monkeypatch):
    store = RunStore()
    run = saved_run(store)
    original_lock = store.lock
    competitor = contextlib.ExitStack()
    refreshed = []

    @contextlib.contextmanager
    def lock(*args, **kwargs):
        with original_lock(*args, **kwargs):
            yield
        # Simulate another command acquiring the lock as soon as it is released.
        competitor.enter_context(original_lock(run["run_id"]))

    def refresh(*args):
        refreshed.append(True)
        with pytest.raises(RunBusy):
            with original_lock(run["run_id"]):
                pytest.fail("the poll must keep its lock")
        raise KeyboardInterrupt

    monkeypatch.setattr(store, "lock", lock)
    monkeypatch.setattr(monitor_mod, "refresh", refresh)
    monkeypatch.setattr(monitor_mod, "MONITOR_LOCK_WAIT_SECONDS", 0)
    monkeypatch.setattr(monitor_mod, "save_remote_logs", lambda *a: None)
    monkeypatch.setattr(monitor_mod, "down", lambda *a: pytest.fail("contention must not destroy a run"))
    try:
        assert monitor_mod.monitor(store, run, None, Creds("invented-account-key")) == 0
    finally:
        competitor.close()
    assert refreshed == [True]


@pytest.mark.parametrize("cleanup_code", [0, 7])
def test_status_watch_keeps_watchdog_evidence_when_it_takes_over(monkeypatch, capsys, cleanup_code):
    store = RunStore()
    run = saved_run(store)
    keys.secret_set("accounts/vast", "invented-account-key")

    def refresh(store_, record, *args):
        record.update(marketplace_state="running", spend_estimate_usd=record["cap_usd"],
                      stop_reason="watchdog_cap", heartbeat={"at": 1000, "action": "destroy_cap"})

    def down(store_, record, *args):
        record["state"] = "termination_requested" if cleanup_code else "billing_stopped_confirmed"
        store_.save(record)
        return cleanup_code

    monkeypatch.setattr(status_mod, "refresh", refresh)
    monkeypatch.setattr(status_mod, "down", down)
    assert main(["status", run["run_id"], "--watch", "--json"], environ={}) == (cleanup_code or 8)
    assert json.loads(capsys.readouterr().out)["runs"][0]["stop_reason"] == "watchdog_cap"


def test_up_refuses_an_incomplete_quote_before_any_create(monkeypatch):
    launch_environment(monkeypatch)
    original_plan = up_mod.run_plan

    def incomplete(*args, **kwargs):
        return replace(original_plan(*args, **kwargs), complete=False)

    monkeypatch.setattr(up_mod, "run_plan", incomplete)
    monkeypatch.setattr(up_mod, "monitor", lambda *a, **kw: pytest.fail("an incomplete quote cannot rent"))
    with respx.mock(assert_all_called=False) as router:
        create, scoped = launch_routes(router)
        assert main(["up", MODEL, "--max-spend", "5", "--yes"], environ={}) == 5
        assert create.call_count == scoped.call_count == 0
    assert RunStore().all() == []


def test_up_refuses_an_offer_without_allocated_disk_before_any_create(monkeypatch):
    launch_environment(monkeypatch)
    original_plan = up_mod.run_plan

    def no_disk(*args, **kwargs):
        report = original_plan(*args, **kwargs)
        ranked = replace(report.ranked[0], offer=replace(report.ranked[0].offer, allocated_disk_gb=None))
        return replace(report, ranked=[ranked])

    monkeypatch.setattr(up_mod, "run_plan", no_disk)
    monkeypatch.setattr("ablit.backends.vast.VastBackend.requote", lambda self, offer, creds: offer)
    with respx.mock(assert_all_called=False) as router:
        create, scoped = launch_routes(router)
        assert main(["up", MODEL, "--max-spend", "5", "--yes"], environ={}) == 5
        assert create.call_count == scoped.call_count == 0
    assert RunStore().all() == []
