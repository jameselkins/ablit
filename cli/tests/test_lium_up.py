"""The paid `up`, `down` and `plan` paths for Lium, against invented responses.

Nothing here shows that renting works on a real Lium machine, because none has been rented.
"""

import json
import threading
import time

import httpx
import pytest
import respx

from ablit import credentials as keys
from ablit.cli import main
from ablit.runs import RunStore
from ablit.runs import up as up_mod
from ablit.runs.lifecycle import local_stop_needed
from ablit.runs.ssh import SSH
from ablit.watchdog import ablit_watchdog as watchdog
from market_fixtures import VAST_URL, FakeVast, vast_row
from test_lium import FEED, feed, node
from test_lium_lifecycle import NODE, PODS, POD, RENT, TEMPLATE, TEMPLATES, error, listing, made, pod, rented
from test_run_commands import tty

MODEL = "llama31-8b-abliterated-q4km"
ENV = {"LIUM_API_KEY": "invented-lium-key"}


def launch_environment(monkeypatch):
    tty(monkeypatch)
    monkeypatch.setattr(up_mod.engines, "check_artifacts", lambda entry: None)
    monkeypatch.setattr(SSH, "identity_public", lambda self: "ssh-ed25519 invented-public-content")
    monkeypatch.setattr(up_mod, "monitor", lambda *args, **kwargs: 0)
    monkeypatch.setattr("builtins.input", lambda prompt: "rent")


def run_pod(request):
    """The pod list answer for the one run in the store, with the generated name."""
    return listing(pod(name="ablit-" + RunStore().all()[0]["run_id"]))


def nodes(price=0.40):
    # The 8B entry needs little RAM and disk, and this node passes its other floors.
    return feed(node(price_per_gpu_hour=price, price_per_node_hour=price))


def test_up_with_backends_lium_rents_a_pod_and_saves_only_nonsecret_records(monkeypatch):
    launch_environment(monkeypatch)
    with respx.mock(assert_all_called=False) as router:
        router.get(FEED).mock(return_value=nodes())
        router.post(TEMPLATES).mock(return_value=made())
        create = router.post(RENT).mock(return_value=rented())
        router.get(PODS).mock(side_effect=run_pod)
        code = main(["up", MODEL, "--untested", "--max-spend", "5", "--backends", "lium"], environ=ENV)
        assert code == 0 and create.call_count == 1
        assert not any(call.request.url.host == "console.vast.ai" for call in router.calls)
        # The feed needs no key, so the key goes only to the account calls.
        assert all("x-api-key" not in call.request.headers for call in router.calls if call.request.url.path == "/api/public/v1/nodes")
    body = json.loads(create.calls[0].request.content)
    assert body["pod_name"].startswith("ablit-") and body["template_id"] == TEMPLATE
    assert body["user_public_key"] == "ssh-ed25519 invented-public-content" and body["termination_hours"] == 12
    record = RunStore().all()[0]
    assert record["backend"] == "lium" and record["resource_ids"] == [POD] and record["offer"]["host_id"] == NODE
    assert record["local_cap_primary"] is True and not record.get("watchdog_key_id") and "ssh_key_id" not in record
    key = keys.secret_get(record["run_id"])
    raw = RunStore().path(record["run_id"]).read_text()
    assert key not in raw and "invented-lium-key" not in raw
    assert keys.secret_get("watchdog/" + record["run_id"]) is None


def test_the_default_choice_includes_lium_when_a_lium_key_exists_and_lium_is_cheaper(monkeypatch):
    launch_environment(monkeypatch)
    keys.secret_set("accounts/vast", "invented-vast-key")
    keys.secret_set("accounts/lium", "invented-lium-key")
    with respx.mock(assert_all_called=False) as router:
        router.post(VAST_URL).mock(side_effect=FakeVast([vast_row(7101, base=2.0)]))
        router.get(FEED).mock(return_value=nodes(0.20))
        router.post(TEMPLATES).mock(return_value=made())
        create = router.post(RENT).mock(return_value=rented())
        router.get(PODS).mock(side_effect=run_pod)
        assert main(["up", MODEL, "--untested", "--max-spend", "5"], environ={}) == 0
        assert create.call_count == 1
    assert RunStore().all()[0]["backend"] == "lium"


def test_the_default_choice_stays_on_vast_when_there_is_no_lium_key(monkeypatch):
    launch_environment(monkeypatch)
    keys.secret_set("accounts/vast", "invented-vast-key")
    with respx.mock(assert_all_called=False) as router:
        router.post(VAST_URL).mock(side_effect=FakeVast([vast_row(7101)]))
        feed_route = router.get(FEED).mock(return_value=nodes(0.01))
        router.post("https://console.vast.ai/api/v0/ssh/").respond(200, json={"success": True, "key": {"id": 7501}})
        router.put("https://console.vast.ai/api/v0/asks/7101/").respond(200, json={"success": True, "new_contract": 7201})
        router.post("https://console.vast.ai/api/v0/auth/apikeys/").respond(200, json={"id": 7401, "key": "invented-token"})
        router.get("https://console.vast.ai/api/v1/instances/").mock(side_effect=lambda request: httpx.Response(
            200, json={"success": True, "next_token": None, "instances": [
                {"id": 7201, "label": "ablit-" + RunStore().all()[0]["run_id"], "actual_status": "running",
                 "ssh_host": "h.example.invalid", "ssh_port": 2201, "host_id": 7301, "dph_total": 1.0, "volume_info": []}]}))
        assert main(["up", MODEL, "--untested", "--max-spend", "5"], environ={}) == 0
        assert feed_route.call_count == 0
    assert RunStore().all()[0]["backend"] == "vast"


def test_a_lium_key_alone_searches_lium_alone(monkeypatch):
    launch_environment(monkeypatch)
    with respx.mock(assert_all_called=False) as router:
        router.get(FEED).mock(return_value=nodes())
        router.post(TEMPLATES).mock(return_value=made())
        router.post(RENT).mock(return_value=rented())
        router.get(PODS).mock(side_effect=run_pod)
        assert main(["up", MODEL, "--untested", "--max-spend", "5"], environ=ENV) == 0
    assert RunStore().all()[0]["backend"] == "lium"


def test_a_price_change_between_quote_and_rent_cancels_a_lium_run(monkeypatch, capsys):
    launch_environment(monkeypatch)
    answers = iter([nodes(0.20), nodes(0.35)])
    with respx.mock(assert_all_called=False) as router:
        router.get(FEED).mock(side_effect=lambda request: next(answers))
        template = router.post(TEMPLATES).mock(return_value=made())
        code = main(["up", MODEL, "--untested", "--max-spend", "5", "--backends", "lium"], environ=ENV)
    assert code == 5 and template.call_count == 0
    assert "price changed" in capsys.readouterr().out


def test_a_node_taken_between_quote_and_rent_cancels_a_lium_run(monkeypatch, capsys):
    launch_environment(monkeypatch)
    answers = iter([nodes(), feed()])
    with respx.mock(assert_all_called=False) as router:
        router.get(FEED).mock(side_effect=lambda request: next(answers))
        rent = router.post(RENT).mock(return_value=rented())
        assert main(["up", MODEL, "--untested", "--max-spend", "5", "--backends", "lium"], environ=ENV) == 5
    assert rent.call_count == 0 and RunStore().all() == []


def test_a_refused_rent_ends_the_run_with_cleanup_confirmed(monkeypatch, capsys):
    launch_environment(monkeypatch)
    with respx.mock(assert_all_called=False) as router:
        router.get(FEED).mock(return_value=nodes())
        router.post(TEMPLATES).mock(return_value=made())
        create = router.post(RENT).mock(return_value=error(403, "insufficient_balance", "balance too low"))
        router.delete(TEMPLATES + "/" + TEMPLATE).mock(return_value=httpx.Response(204))
        router.get(PODS).mock(return_value=listing())
        code = main(["up", MODEL, "--untested", "--max-spend", "5", "--backends", "lium"], environ=ENV)
    assert code == 6 and create.call_count == 1
    record = RunStore().all()[0]
    assert record["create_rejected"] is True and record["state"] == "billing_stopped_confirmed"
    assert "insufficient_balance" in capsys.readouterr().err


def test_a_failed_template_ends_the_run_without_a_rent(monkeypatch):
    launch_environment(monkeypatch)
    with respx.mock(assert_all_called=False) as router:
        router.get(FEED).mock(return_value=nodes())
        router.post(TEMPLATES).mock(return_value=error(400, "image_not_found"))
        rent = router.post(RENT).mock(return_value=rented())
        router.get(PODS).mock(return_value=listing())
        code = main(["up", MODEL, "--untested", "--max-spend", "5", "--backends", "lium"], environ=ENV)
    assert code == 6 and rent.call_count == 0
    assert RunStore().all()[0]["state"] == "billing_stopped_confirmed"


def saved_lium_run(store):
    from test_lium_lifecycle import OFFER
    from test_run_commands import saved_run
    import test_vast_lifecycle
    original = test_vast_lifecycle.OFFER
    test_vast_lifecycle.OFFER = OFFER
    try:
        data = saved_run(store)
    finally:
        test_vast_lifecycle.OFFER = original
    data.update(resource_ids=[POD], ssh_host="203.0.113.7", ssh_port=34446, watchdog_key_id=None,
                watchdog_key_sent=False)
    store.save(data)
    return data


def test_down_deletes_the_pod_and_confirms_billing_stopped(capsys):
    store = RunStore()
    run = saved_lium_run(store)
    assert run["backend"] == "lium"
    keys.secret_set(run["run_id"], "invented-endpoint-key")
    # The first list finds the pod by name, and the second one, after the delete, shows it gone.
    states = iter([listing(pod(name="ablit-" + run["run_id"])), listing()])
    with respx.mock as router:
        router.get(PODS).mock(side_effect=lambda request: next(states))
        delete = router.delete(PODS + "/" + POD).mock(return_value=httpx.Response(204))
        code = main(["down", run["run_id"]], environ=ENV)
        assert delete.call_count == 1
        assert delete.calls[0].request.headers["X-API-Key"] == "invented-lium-key"
    assert code == 0
    assert "Billing stopped" in capsys.readouterr().out
    assert store.load(run["run_id"])["state"] == "billing_stopped_confirmed"
    assert keys.secret_get(run["run_id"]) is None


def test_down_stays_pending_while_the_pod_is_still_listed_as_charged(capsys):
    store = RunStore()
    run = saved_lium_run(store)
    with respx.mock as router:
        router.get(PODS).mock(return_value=listing(pod(name="ablit-" + run["run_id"], status="RUNNING")))
        router.delete(PODS + "/" + POD).mock(return_value=httpx.Response(204))
        assert main(["down", run["run_id"]], environ=ENV) == 7
    out = capsys.readouterr().out
    assert "Pods are still in the user's pod list" in out and "lium.io" in out
    assert store.load(run["run_id"])["state"] != "billing_stopped_confirmed"


def test_the_lium_run_uses_the_local_check_even_when_the_heartbeat_is_fresh():
    run = saved_lium_run(RunStore())
    now = time.time()
    run.update(marketplace_state="running", heartbeat={"at": now, "spend": 0, "action": "continue"},
               spend_estimate_usd=run["cap_usd"])
    assert local_stop_needed(run, now) is True


def test_plan_adds_the_local_check_note_when_lium_is_chosen_and_not_otherwise(capsys):
    with respx.mock(assert_all_called=False) as router:
        router.post(VAST_URL).mock(side_effect=FakeVast([vast_row(7101, base=2.0)]))
        router.get(FEED).mock(return_value=nodes(0.20))
        assert main(["plan", MODEL, "--max-spend", "5", "--backends", "vast,lium"], environ={}) == 0
    out = capsys.readouterr().out
    assert "Chosen offer" in out and "On Lium, a key made with your key cannot be limited" in out
    with respx.mock(assert_all_called=False) as router:
        router.post(VAST_URL).mock(side_effect=FakeVast([vast_row(7101, base=0.1)]))
        router.get(FEED).mock(return_value=nodes(2.0))
        assert main(["plan", MODEL, "--max-spend", "5", "--backends", "vast,lium"], environ={}) == 0
    assert "On Lium, a key made with your key" not in capsys.readouterr().out


def test_the_watchdog_config_for_a_lium_run_cannot_destroy_and_says_why(tmp_path):
    config = tmp_path / "run.json"
    config.write_text(json.dumps({"backend": "lium", "instance_id": POD}))
    with pytest.raises(RuntimeError, match="no key limited to one pod"):
        watchdog.destroy_instance("cap", str(config))


def test_a_lium_run_with_no_ssh_command_at_the_start_deadline_denies_its_node(monkeypatch):
    from ablit.runs import monitor as monitor_mod
    store = RunStore()
    run = saved_lium_run(store)
    run.update(state="creating", ssh_host=None, ssh_port=None, started_at=time.time() - 3600, host_id=NODE)
    store.save(run)
    keys.secret_set(run["run_id"], "invented-endpoint-key")
    monkeypatch.setattr(monitor_mod, "down", lambda *args, **kwargs: 0)
    monkeypatch.setattr(monitor_mod.time, "sleep", lambda seconds: None)
    from ablit.backends.lium import LiumBackend
    driver = LiumBackend(httpx.Client(), store=store)
    with respx.mock as router:
        router.get(PODS).mock(return_value=listing(pod(name="ablit-" + run["run_id"], status="PENDING",
                                                       ssh_connect_cmd="")))
        code = monitor_mod.monitor(store, run, driver, keys.Creds("invented-lium-key"), want_ready=True)
    assert code == 6 and store.denied_hosts("lium") == {NODE}
    assert store.denied_hosts("vast") == set()


# Paid tests on 2026-10-26 ran three `up` calls for one model at once, each with its own --backends.
# The Lium call attached to the RunPod call's run, which was still creating, and waited on that pod.

VAST = "https://console.vast.ai"


def vast_routes(router):
    """Answer a Vast rent, and list one instance labeled for each saved Vast run."""
    router.post(VAST_URL).mock(side_effect=FakeVast([vast_row(7101)]))
    router.post(VAST + "/api/v0/ssh/").respond(200, json={"success": True, "key": {"id": 7501}})
    create = router.put(VAST + "/api/v0/asks/7101/").respond(200, json={"success": True, "new_contract": 7201})
    router.post(VAST + "/api/v0/auth/apikeys/").respond(200, json={"id": 7401, "key": "invented-token"})
    router.get(VAST + "/api/v1/instances/").mock(side_effect=lambda request: httpx.Response(
        200, json={"success": True, "next_token": None, "instances": [
            {"id": 7201, "label": "ablit-" + r["run_id"], "actual_status": "running",
             "ssh_host": "h.example.invalid", "ssh_port": 2201, "host_id": 7301, "dph_total": 1.0, "volume_info": []}
            for r in RunStore().all() if r["backend"] == "vast"]}))
    return create


def creating_vast_run(router):
    """Leave one Vast run in `creating`, as an `up` that has not reached the engine leaves it."""
    create = vast_routes(router)
    assert main(["up", MODEL, "--untested", "--max-spend", "3", "--backends", "vast", "--yes"], environ={}) == 0
    (record,) = RunStore().all()
    assert record["state"] == "creating" and record["resource_ids"] == ["7201"]
    return create, record


def lium_routes(router):
    router.get(FEED).mock(return_value=nodes())
    router.post(TEMPLATES).mock(return_value=made())
    return router.post(RENT).mock(return_value=rented())


def test_up_for_lium_never_takes_over_a_vast_run_that_is_still_creating(monkeypatch, capsys):
    launch_environment(monkeypatch)
    keys.secret_set("accounts/vast", "invented-vast-key")
    with respx.mock(assert_all_called=False) as router:
        _, vast_run = creating_vast_run(router)
        rent = lium_routes(router)
        capsys.readouterr()
        code = main(["up", MODEL, "--untested", "--max-spend", "3", "--backends", "lium", "--yes"], environ=ENV)
        assert code == 2 and rent.call_count == 0
        # The refusal must not clean up a run that belongs to another `up`.
        assert not [c for c in router.calls if c.request.method == "DELETE"]
    err = capsys.readouterr().err
    assert vast_run["run_id"] in err and "on vast" in err and "searches only lium" in err and "--another" in err
    assert RunStore().load(vast_run["run_id"])["state"] == "creating"


def test_up_never_resumes_a_run_that_a_live_up_still_owns(monkeypatch, capsys):
    launch_environment(monkeypatch)
    keys.secret_set("accounts/vast", "invented-vast-key")
    with respx.mock(assert_all_called=False) as router:
        create, vast_run = creating_vast_run(router)
        capsys.readouterr()
        # A live `up` holds the owner lock for as long as it runs.
        with RunStore().lock(vast_run["run_id"] + ".owner"):
            code = main(["up", MODEL, "--untested", "--max-spend", "3", "--backends", "vast", "--yes"], environ={})
        assert code == 2 and create.call_count == 1
        assert not [c for c in router.calls if c.request.method == "DELETE"]
    assert "Another ablit up is still starting it" in capsys.readouterr().err


def test_a_resumed_run_is_named_with_its_backend_and_state(monkeypatch, capsys):
    launch_environment(monkeypatch)
    keys.secret_set("accounts/vast", "invented-vast-key")
    with respx.mock(assert_all_called=False) as router:
        create, vast_run = creating_vast_run(router)
        capsys.readouterr()
        # The earlier `up` has exited, so this is the documented recovery case.
        assert main(["up", MODEL, "--untested", "--max-spend", "3", "--yes"], environ={}) == 0
        assert create.call_count == 1
    assert f"Resuming run {vast_run['run_id']} on vast, state creating" in capsys.readouterr().err


def test_up_waits_briefly_for_a_parallel_up_to_release_the_selection_lock(monkeypatch, capsys):
    launch_environment(monkeypatch)
    monkeypatch.setattr(up_mod, "SELECTION_LOCK_WAIT_SECONDS", 10.0)
    keys.secret_set("accounts/vast", "invented-vast-key")
    held, release = threading.Event(), threading.Event()

    def other_up():
        with RunStore().lock("selection"):
            held.set()
            release.wait(5)

    worker = threading.Thread(target=other_up)
    worker.start()
    held.wait(5)
    threading.Timer(0.5, release.set).start()
    with respx.mock(assert_all_called=False) as router:
        create = vast_routes(router)
        assert main(["up", MODEL, "--untested", "--max-spend", "3", "--backends", "vast", "--yes"], environ={}) == 0
        assert create.call_count == 1
    worker.join()
    assert "Waiting up to 10 seconds" in capsys.readouterr().err


def test_up_says_what_to_do_when_the_selection_lock_stays_busy(monkeypatch, capsys):
    launch_environment(monkeypatch)
    monkeypatch.setattr(up_mod, "SELECTION_LOCK_WAIT_SECONDS", 0.3, raising=False)
    keys.secret_set("accounts/vast", "invented-vast-key")
    with respx.mock(assert_all_called=False) as router:
        create = vast_routes(router)
        with RunStore().lock("selection"):
            code = main(["up", MODEL, "--untested", "--max-spend", "3", "--backends", "vast", "--yes"], environ={})
        assert code == 2 and create.call_count == 0
    assert "Wait until it prints its run ID or exits, then run up again" in capsys.readouterr().err
