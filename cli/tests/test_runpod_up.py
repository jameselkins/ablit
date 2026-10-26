"""The paid `up`, `down`, `setup` and `plan` paths for RunPod, against invented responses."""

import json
import sys

import httpx
import pytest
import respx

from ablit import credentials as keys
from ablit.backends.runpod import RunPodBackend
from ablit.cli import main
from ablit.runs import RunStore
from ablit.runs import up as up_mod
from ablit.runs.lifecycle import local_stop_needed
from ablit.runs.ssh import SSH
from ablit.watchdog import ablit_watchdog as watchdog
from market_fixtures import RUNPOD_URL, VAST_URL, FakeRunPod, FakeVast, runpod_type, vast_row
from test_runpod_lifecycle import GPU, PODS, listing, pod
from test_run_commands import tty

MODEL = "llama31-8b-abliterated-q4km"


def launch_environment(monkeypatch):
    tty(monkeypatch)
    monkeypatch.setattr(up_mod.engines, "check_artifacts", lambda entry: None)
    monkeypatch.setattr(SSH, "identity_public", lambda self: "ssh-ed25519 invented-public-content")
    monkeypatch.setattr(up_mod, "monitor", lambda *args, **kwargs: 0)
    monkeypatch.setattr("builtins.input", lambda prompt: "rent")


def run_pod(request):
    """The pod list answer for the one run in the store, with the generated name."""
    return listing(pod(name="ablit-" + RunStore().all()[0]["run_id"], **{"disk": 600, "cost": 0.20}))


def runpod_types(price=0.20):
    # The 8B entry needs little RAM and disk, and the 4090 passes its other floors.
    return [runpod_type(GPU, "RTX 4090", 24, price=price, pod_ram=100)]


def test_setup_runpod_reads_hidden_key_and_tests_with_one_read_only_call(monkeypatch, capsys):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)
    answers = iter(["yes", "yes"])
    monkeypatch.setattr("builtins.input", lambda prompt: next(answers))
    monkeypatch.setattr(keys.getpass, "getpass", lambda prompt: "invented-runpod-setup-key")
    with respx.mock as router:
        route = router.get(PODS).mock(return_value=listing())
        assert main(["setup", "runpod"], environ={}) == 0
        assert route.call_count == 1 and route.calls[0].request.method == "GET"
        assert route.calls[0].request.headers["Authorization"] == "Bearer invented-runpod-setup-key"
    assert keys.secret_get("accounts/runpod") == "invented-runpod-setup-key"
    assert "invented-runpod-setup-key" not in capsys.readouterr().out


def test_setup_runpod_session_choice_never_saves_the_key(monkeypatch, capsys):
    answers = iter(["yes", "no"])
    monkeypatch.setattr("builtins.input", lambda prompt: next(answers))
    monkeypatch.setattr(keys.getpass, "getpass", lambda prompt: "invented-session-key")
    with respx.mock as router:
        router.get(PODS).mock(return_value=listing())
        assert keys.setup_runpod(RunPodBackend(httpx.Client()), RunStore(), False, {}) == 0
    assert keys.secret_get("accounts/runpod") is None and keys.SESSION["runpod"] == "invented-session-key"
    assert "RUNPOD_API_KEY" in capsys.readouterr().out


def test_setup_runpod_has_no_paid_link_because_ids_toml_has_no_runpod_id(monkeypatch, capsys):
    answers = iter(["no", "2"])
    monkeypatch.setattr("builtins.input", lambda prompt: next(answers))
    with respx.mock:
        assert keys.setup_runpod(RunPodBackend(httpx.Client()), RunStore(), False, {}) == 0
    out = capsys.readouterr().out
    assert "Sign up directly" in out and "Skip" in out and "paid link" not in out and "ref=" not in out


def test_the_runpod_environment_key_wins_over_the_keyring():
    keys.secret_set("accounts/runpod", "invented-keyring-key")
    assert keys.credentials("runpod", {"RUNPOD_API_KEY": "invented-env-key"}).api_key == "invented-env-key"
    assert keys.credentials("runpod", {}).api_key == "invented-keyring-key"


def test_up_with_backends_runpod_rents_a_pod_and_saves_only_nonsecret_records(monkeypatch):
    launch_environment(monkeypatch)
    with respx.mock(assert_all_called=False) as router:
        router.post(RUNPOD_URL).mock(side_effect=FakeRunPod(runpod_types()))
        create = router.post(PODS).mock(return_value=httpx.Response(201, json=pod(disk=600, cost=0.20)))
        router.get(PODS).mock(side_effect=run_pod)
        code = main(["up", MODEL, "--untested", "--max-spend", "5", "--backends", "runpod"],
                    environ={"RUNPOD_API_KEY": "invented-runpod-key"})
        assert code == 0 and create.call_count == 1
        assert not any(call.request.url.host == "console.vast.ai" for call in router.calls)
        assert not any(call.request.url.path.endswith("/action") for call in router.calls)
    body = json.loads(create.calls[0].request.content)
    assert body["gpu"]["id"] == GPU and body["name"].startswith("ablit-") and body["env"] == {
        "PUBLIC_KEY": "ssh-ed25519 invented-public-content"}
    record = RunStore().all()[0]
    assert record["backend"] == "runpod" and record["resource_ids"] == ["pod1"]
    assert record["local_cap_primary"] is True and not record.get("watchdog_key_id") and "ssh_key_id" not in record
    key = keys.secret_get(record["run_id"])
    raw = RunStore().path(record["run_id"]).read_text()
    assert key not in raw and "invented-runpod-key" not in raw
    assert keys.secret_get("watchdog/" + record["run_id"]) is None


def test_the_default_choice_includes_runpod_when_a_runpod_key_exists_and_runpod_is_cheaper(monkeypatch):
    launch_environment(monkeypatch)
    keys.secret_set("accounts/vast", "invented-vast-key")
    keys.secret_set("accounts/runpod", "invented-runpod-key")
    with respx.mock(assert_all_called=False) as router:
        router.post(VAST_URL).mock(side_effect=FakeVast([vast_row(7101, base=2.0)]))
        router.post(RUNPOD_URL).mock(side_effect=FakeRunPod(runpod_types(0.20)))
        create = router.post(PODS).mock(return_value=httpx.Response(201, json=pod(disk=600, cost=0.20)))
        router.get(PODS).mock(side_effect=run_pod)
        assert main(["up", MODEL, "--untested", "--max-spend", "5"], environ={}) == 0
        assert create.call_count == 1
    assert RunStore().all()[0]["backend"] == "runpod"


def test_the_default_choice_stays_on_vast_when_there_is_no_runpod_key(monkeypatch):
    launch_environment(monkeypatch)
    keys.secret_set("accounts/vast", "invented-vast-key")
    with respx.mock(assert_all_called=False) as router:
        router.post(VAST_URL).mock(side_effect=FakeVast([vast_row(7101)]))
        runpod_search = router.post(RUNPOD_URL).mock(side_effect=FakeRunPod(runpod_types(0.01)))
        router.post("https://console.vast.ai/api/v0/ssh/").respond(200, json={"success": True, "key": {"id": 7501}})
        router.put("https://console.vast.ai/api/v0/asks/7101/").respond(200, json={"success": True, "new_contract": 7201})
        router.post("https://console.vast.ai/api/v0/auth/apikeys/").respond(200, json={"id": 7401, "key": "invented-token"})
        router.get("https://console.vast.ai/api/v1/instances/").mock(side_effect=lambda request: httpx.Response(
            200, json={"success": True, "next_token": None, "instances": [
                {"id": 7201, "label": "ablit-" + RunStore().all()[0]["run_id"], "actual_status": "running",
                 "ssh_host": "h.example.invalid", "ssh_port": 2201, "host_id": 7301, "dph_total": 1.0, "volume_info": []}]}))
        assert main(["up", MODEL, "--untested", "--max-spend", "5"], environ={}) == 0
        assert runpod_search.call_count == 0
    assert RunStore().all()[0]["backend"] == "vast"


def test_a_runpod_key_alone_searches_runpod_alone(monkeypatch):
    launch_environment(monkeypatch)
    with respx.mock(assert_all_called=False) as router:
        router.post(RUNPOD_URL).mock(side_effect=FakeRunPod(runpod_types()))
        router.post(PODS).mock(return_value=httpx.Response(201, json=pod(disk=600, cost=0.20)))
        router.get(PODS).mock(side_effect=run_pod)
        assert main(["up", MODEL, "--untested", "--max-spend", "5"], environ={"RUNPOD_API_KEY": "invented"}) == 0
    assert RunStore().all()[0]["backend"] == "runpod"


@pytest.mark.parametrize("flag", ["lambda", "vast,lambda", ","])
def test_up_refuses_a_backend_it_cannot_rent_on(monkeypatch, flag, capsys):
    launch_environment(monkeypatch)
    with respx.mock:
        assert main(["up", MODEL, "--untested", "--max-spend", "5", "--backends", flag],
                    environ={"RUNPOD_API_KEY": "invented"}) == 2
    assert "support only --backends" in capsys.readouterr().err
    assert RunStore().all() == []


def test_a_price_change_between_quote_and_rent_cancels_a_runpod_run(monkeypatch, capsys):
    launch_environment(monkeypatch)
    fake = FakeRunPod(runpod_types(0.20))
    state = {"calls": 0}

    def drifting(request):
        state["calls"] += 1
        if state["calls"] > 2:  # the two search queries come first, then the requote
            fake.any_cloud = fake.secure = runpod_types(0.35)
        return fake(request)

    with respx.mock(assert_all_called=False) as router:
        router.post(RUNPOD_URL).mock(side_effect=drifting)
        create = router.post(PODS).mock(return_value=httpx.Response(201, json=pod()))
        code = main(["up", MODEL, "--untested", "--max-spend", "5", "--backends", "runpod"],
                    environ={"RUNPOD_API_KEY": "invented"})
    assert code == 5 and create.call_count == 0
    assert "price changed" in capsys.readouterr().out


def test_a_pod_that_differs_from_the_quote_is_destroyed_by_up(monkeypatch):
    launch_environment(monkeypatch)
    listed = {"pods": []}

    def listing_now(request):
        record = RunStore().all()[0]
        return listing(pod(name="ablit-" + record["run_id"], cost=0.99, disk=600)) if listed["pods"] == [] else listing()

    with respx.mock(assert_all_called=False) as router:
        router.post(RUNPOD_URL).mock(side_effect=FakeRunPod(runpod_types()))
        router.post(PODS).mock(return_value=httpx.Response(201, json=pod(cost=0.99, disk=600)))
        router.get(PODS).mock(side_effect=listing_now)

        def deleted(request):
            listed["pods"].append("gone")
            return httpx.Response(204)

        delete = router.delete(PODS + "/pod1").mock(side_effect=deleted)
        code = main(["up", MODEL, "--untested", "--max-spend", "5", "--backends", "runpod"],
                    environ={"RUNPOD_API_KEY": "invented"})
    assert code == 6 and delete.call_count == 1
    assert RunStore().all()[0]["state"] == "billing_stopped_confirmed"


def saved_runpod_run(store):
    from test_run_commands import saved_run
    from test_runpod_lifecycle import OFFER
    import test_vast_lifecycle
    original = test_vast_lifecycle.OFFER
    test_vast_lifecycle.OFFER = OFFER
    try:
        data = saved_run(store)
    finally:
        test_vast_lifecycle.OFFER = original
    data.update(resource_ids=["pod1"], ssh_host="203.0.113.7", ssh_port=34446, watchdog_key_id=None,
                watchdog_key_sent=False)
    store.save(data)
    return data


def test_down_terminates_the_pod_and_confirms_billing_stopped(monkeypatch, capsys):
    store = RunStore()
    run = saved_runpod_run(store)
    assert run["backend"] == "runpod"
    keys.secret_set(run["run_id"], "invented-endpoint-key")
    # The first list finds the pod by name, and the second one, after the delete, shows it gone.
    states = iter([listing(pod(name="ablit-" + run["run_id"])), listing()])
    with respx.mock as router:
        router.get(PODS).mock(side_effect=lambda request: next(states))
        delete = router.delete(PODS + "/pod1").mock(return_value=httpx.Response(204))
        code = main(["down", run["run_id"]], environ={"RUNPOD_API_KEY": "invented-runpod-key"})
        assert delete.call_count == 1
        assert delete.calls[0].request.headers["Authorization"] == "Bearer invented-runpod-key"
    assert code == 0
    assert "Billing stopped" in capsys.readouterr().out
    assert store.load(run["run_id"])["state"] == "billing_stopped_confirmed"
    assert keys.secret_get(run["run_id"]) is None


def test_down_stays_pending_while_the_pod_is_still_listed(monkeypatch, capsys):
    store = RunStore()
    run = saved_runpod_run(store)
    with respx.mock as router:
        router.get(PODS).mock(return_value=listing(pod(name="ablit-" + run["run_id"], status="EXITED")))
        router.delete(PODS + "/pod1").mock(return_value=httpx.Response(204))
        assert main(["down", run["run_id"]], environ={"RUNPOD_API_KEY": "invented"}) == 7
    out = capsys.readouterr().out
    assert "Pods are still in the user's pod list" in out and "console.runpod.io" in out
    assert store.load(run["run_id"])["state"] != "billing_stopped_confirmed"


def test_the_runpod_run_uses_the_local_check_even_when_the_heartbeat_is_fresh():
    store = RunStore()
    run = saved_runpod_run(store)
    import time
    now = time.time()
    run.update(marketplace_state="running", heartbeat={"at": now, "spend": 0, "action": "continue"},
               spend_estimate_usd=run["cap_usd"])
    assert local_stop_needed(run, now) is True
    run["local_cap_primary"] = False
    assert local_stop_needed(run, now) is True


def test_a_runpod_offer_with_no_advertised_speed_does_not_break_the_cap_math():
    from test_runpod_lifecycle import OFFER
    from ablit.select.request import request_from_entry
    from ablit.catalog import load_catalog, resolve
    entry = resolve(load_catalog().entries, MODEL)
    req = request_from_entry(entry, 10.0)
    from dataclasses import replace
    offer = replace(OFFER, download_mbps=None)
    assert up_mod.minimum_cap(offer, req, 500.0) > 0


def test_plan_adds_the_local_check_note_when_runpod_is_chosen_and_not_otherwise(capsys):
    with respx.mock(assert_all_called=False) as router:
        router.post(VAST_URL).mock(side_effect=FakeVast([vast_row(7101, base=2.0)]))
        router.post(RUNPOD_URL).mock(side_effect=FakeRunPod(runpod_types(0.20)))
        assert main(["plan", MODEL, "--max-spend", "5", "--backends", "vast,runpod"], environ={}) == 0
    out = capsys.readouterr().out
    assert "Chosen offer" in out and "On RunPod, ablit cannot create a key" in out
    with respx.mock(assert_all_called=False) as router:
        router.post(VAST_URL).mock(side_effect=FakeVast([vast_row(7101, base=0.1)]))
        router.post(RUNPOD_URL).mock(side_effect=FakeRunPod(runpod_types(2.0)))
        assert main(["plan", MODEL, "--max-spend", "5", "--backends", "vast,runpod"], environ={}) == 0
    assert "On RunPod, ablit cannot create a key" not in capsys.readouterr().out


def test_the_watchdog_destroys_a_runpod_pod_with_the_pod_key_and_refuses_a_bad_id(monkeypatch):
    sent = []

    class Reply:
        def __enter__(self): return self
        def __exit__(self, *args): return False

    def fake_urlopen(request, timeout):
        sent.append((request.get_method(), request.full_url, request.get_header("Authorization")))
        return Reply()

    monkeypatch.setattr(watchdog.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setenv("RUNPOD_API_KEY", "invented-pod-key")
    watchdog.destroy_runpod_pod("pod1")
    assert sent == [("DELETE", "https://api.runpod.io/v2/pods/pod1", "Bearer invented-pod-key")]
    with pytest.raises(ValueError):
        watchdog.destroy_runpod_pod("../account")
    monkeypatch.delenv("RUNPOD_API_KEY")
    monkeypatch.setattr("builtins.open", lambda *a, **k: (_ for _ in ()).throw(OSError("no file")))
    with pytest.raises(RuntimeError, match="No RunPod pod key"):
        watchdog.destroy_runpod_pod("pod1")


def test_the_watchdog_config_routes_a_runpod_run_to_the_runpod_call(monkeypatch, tmp_path):
    called = []
    monkeypatch.setattr(watchdog, "destroy_runpod_pod", called.append)
    config = tmp_path / "run.json"
    config.write_text(json.dumps({"backend": "runpod", "instance_id": "pod1"}))
    watchdog.destroy_instance("cap", str(config))
    assert called == ["pod1"]
