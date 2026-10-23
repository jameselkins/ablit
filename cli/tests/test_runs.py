"""Durable records and retryable cleanup use synthetic resources."""

import json
import os
from dataclasses import asdict
from pathlib import Path

import httpx
import pytest
import respx

from ablit.backends.vast import VastBackend
from ablit.runs import RunError, RunStore
from ablit.runs.lifecycle import down, local_stop_needed
from ablit.runs.store import atomic_json
from test_vast_lifecycle import ROOT, RUN, KEY, instance, page, OFFER


def record(store, **updates):
    data = {"schema": 1, "run_id": RUN, "model_id": "invented-model", "state": "running",
            "offer": asdict(OFFER), "resource_ids": ["7201"], "started_at": 1000,
            "cap_usd": 2, "create_sent": True, "watchdog_key_sent": True,
            "watchdog_key_id": "7401"}
    data.update(updates)
    store.save(data)
    return data


def test_atomic_replace_flushes_file_before_rename_and_directory_after(tmp_path, monkeypatch):
    path = tmp_path / "run.json"
    atomic_json(path, {"old": True})
    original_replace, original_fsync = os.replace, os.fsync
    calls = []
    def fsync(fd):
        calls.append("fsync")
        original_fsync(fd)
    def replace(source, destination):
        calls.append("replace")
        assert json.loads(path.read_text()) == {"old": True}
        assert json.loads(Path(source).read_text()) == {"new": True}
        original_replace(source, destination)
    monkeypatch.setattr(os, "replace", replace)
    monkeypatch.setattr(os, "fsync", fsync)
    atomic_json(path, {"new": True})
    assert calls == ["fsync", "replace", "fsync"]
    assert path.stat().st_mode & 0o777 == 0o600
    assert not list(tmp_path.glob(".pending-*"))


def test_failed_replace_leaves_old_record_and_removes_temp(tmp_path, monkeypatch):
    path = tmp_path / "run.json"
    atomic_json(path, {"old": True})
    def fail(*args):
        raise OSError("invented rename failure")
    monkeypatch.setattr(os, "replace", fail)
    with pytest.raises(OSError):
        atomic_json(path, {"new": True})
    assert json.loads(path.read_text()) == {"old": True}
    assert not list(tmp_path.glob(".pending-*"))


def test_per_run_lock_refuses_a_second_holder_and_releases_after_error(tmp_path):
    store = RunStore(tmp_path)
    with pytest.raises(ValueError):
        with store.lock(RUN):
            with pytest.raises(RunError, match="busy"):
                with RunStore(tmp_path).lock(RUN):
                    pytest.fail("lock was not exclusive")
            with store.lock("another-run"):
                pass
            raise ValueError("invented")
    with store.lock(RUN):
        pass


def test_paths_and_model_selection(tmp_path):
    store = RunStore(tmp_path)
    with pytest.raises(RunError):
        store.path("../escape")
    r = record(store)
    assert store.active_model("invented-model") == [r]
    r["state"] = "billing_stopped_confirmed"
    store.save(r)
    assert not store.active_model("invented-model")
    assert store.select(None) == r


def test_deny_list_is_local_and_durable(tmp_path):
    store = RunStore(tmp_path)
    store.deny("invented-host", "speed_below_floor")
    assert RunStore(tmp_path).denied_hosts() == {"invented-host"}


def test_down_partial_failure_and_retry_preserve_state_and_resources(tmp_path, capsys):
    store = RunStore(tmp_path)
    r = record(store)
    driver = VastBackend(httpx.Client(), store=store)
    with respx.mock(assert_all_called=False) as router:
        listing = router.get(ROOT + "/api/v1/instances/").mock(return_value=page([instance()]))
        destroy = router.delete(ROOT + "/api/v0/instances/7201/").respond(503, json={"success": False})
        revoke = router.delete(ROOT + "/api/v0/auth/apikeys/7401/").respond(200, json="Successfully Deleted API Key")
        assert down(store, r, driver, KEY) == 7
        saved = store.load(RUN)
        assert saved["state"] == "termination_requested"
        assert saved["resource_ids"] == ["7201"]
        assert "Billing stopped" not in capsys.readouterr().out
        assert revoke.call_count == 0
        destroy.respond(200, json={"success": True})
        listing.mock(side_effect=[page([instance()]), page([])])
        assert down(store, saved, driver, KEY) == 0
        assert store.load(RUN)["state"] == "billing_stopped_confirmed"
        assert "Billing stopped" in capsys.readouterr().out
        assert revoke.call_count == 1


def test_cleanup_moves_through_all_states_and_retries_key_failure(tmp_path, monkeypatch):
    store = RunStore(tmp_path)
    r = record(store)
    driver = VastBackend(httpx.Client(), store=store)
    states = []
    save = store.save
    def observe_save(data):
        states.append(data["state"])
        save(data)
    monkeypatch.setattr(store, "save", observe_save)
    with respx.mock as router:
        router.get(ROOT + "/api/v1/instances/").mock(return_value=page([]))
        router.delete(ROOT + "/api/v0/instances/7201/").respond(404)
        revoke = router.delete(ROOT + "/api/v0/auth/apikeys/7401/").respond(503)
        assert down(store, r, driver, KEY) == 7
        assert r["state"] == "resources_terminated"
        revoke.respond(200, json="Successfully Deleted API Key")
        assert down(store, store.load(RUN), driver, KEY) == 0
    assert states[0] == "termination_requested"
    assert "resources_terminated" in states
    assert states[-1] == "billing_stopped_confirmed"


def test_down_recovers_lost_create_by_label(tmp_path):
    store = RunStore(tmp_path)
    r = record(store, resource_ids=[], state="creating", watchdog_key_sent=False, watchdog_key_id=None)
    driver = VastBackend(httpx.Client(), store=store)
    with respx.mock as router:
        router.get(ROOT + "/api/v1/instances/").mock(side_effect=[page([instance()]), page([])])
        destroy = router.delete(ROOT + "/api/v0/instances/7201/").respond(200, json={"success": True})
        assert down(store, r, driver, KEY) == 0
        assert destroy.call_count == 1
        assert store.load(RUN)["resource_ids"] == ["7201"]


def test_empty_list_cannot_clear_an_ambiguous_create(tmp_path):
    store = RunStore(tmp_path)
    r = record(store, resource_ids=[], state="creating", watchdog_key_sent=False, watchdog_key_id=None)
    with respx.mock as router:
        router.get(ROOT + "/api/v1/instances/").mock(return_value=page([]))
        assert down(store, r, VastBackend(httpx.Client()), KEY) == 7
    assert store.load(RUN)["state"] != "billing_stopped_confirmed"


def test_local_check_requires_a_stale_heartbeat_and_cap(tmp_path):
    store = RunStore(tmp_path)
    r = record(store, marketplace_state="running", spend_estimate_usd=3, heartbeat={"at": 1500})
    assert not local_stop_needed(r, 1680)
    assert local_stop_needed(r, 1681)
    r["spend_estimate_usd"] = 0
    assert not local_stop_needed(r, 1681)


def test_no_key_revocation_when_billing_check_times_out(tmp_path):
    store = RunStore(tmp_path)
    r = record(store)
    driver = VastBackend(httpx.Client(), store=store)
    driver._sleep = lambda seconds: None
    with respx.mock(assert_all_called=False) as router:
        # Reads retry after a timeout, so every retry of the billing check times out too.
        router.get(ROOT + "/api/v1/instances/").mock(
            side_effect=[page([instance()])] + [httpx.ReadTimeout("invented")] * 5)
        router.delete(ROOT + "/api/v0/instances/7201/").respond(200, json={"success": True})
        revoke = router.delete(ROOT + "/api/v0/auth/apikeys/7401/").respond(200, json="Successfully Deleted API Key")
        assert down(store, r, driver, KEY) == 7
        assert revoke.call_count == 0
