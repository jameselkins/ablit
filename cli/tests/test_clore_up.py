"""The paid `up`, `down`, `status` and `plan` paths for Clore, against invented responses.

Nothing here shows that renting works on a real Clore machine, because none has been rented.
"""

import json
import time

import httpx
import pytest
import respx

from ablit import credentials as keys
from ablit.backends.clore import API_ROOT, CloreBackend
from ablit.cli import main
from ablit.runs import RunStore
from ablit.runs import monitor as monitor_mod, up as up_mod
from ablit.runs.lifecycle import local_stop_needed
from ablit.runs.ssh import SSH
from ablit.watchdog import ablit_watchdog as watchdog
from market_fixtures import VAST_URL, FakeVast, vast_row
from test_clore import server
from test_clore_lifecycle import CANCEL, CREATE, ORDERS, OFFER, OK, order, orders
from test_run_commands import tty

MODEL = "llama31-8b-abliterated-q4km"
MARKET = API_ROOT + "/marketplace"
CLORE_ENV = {"CLORE_API_KEY": "invented-clore-key"}


def launch_environment(monkeypatch):
    tty(monkeypatch)
    monkeypatch.setattr(up_mod.engines, "check_artifacts", lambda entry: None)
    monkeypatch.setattr(SSH, "identity_public", lambda self: "ssh-ed25519 invented-public-content")
    monkeypatch.setattr(up_mod, "monitor", lambda *args, **kwargs: 0)
    monkeypatch.setattr("builtins.input", lambda prompt: "rent")
    monkeypatch.setattr(CloreBackend, "_sleep", staticmethod(lambda seconds: None))
    monkeypatch.setattr(CloreBackend, "request_interval", 0)


def market(usd=4.8):
    return httpx.Response(200, json={"code": 0, "servers": [server(7101, usd=usd)]})


def run_order(**overrides):
    """The order list answer for the one run in the store, with the image that run sent."""
    record = RunStore().all()[0]
    return orders(order(9001, server=7101, image=record["image"], **overrides))


def test_up_with_backends_clore_rents_whole_server_and_saves_only_nonsecret_records(monkeypatch):
    launch_environment(monkeypatch)
    with respx.mock(assert_all_called=False) as router:
        router.get(MARKET).mock(side_effect=lambda request: market())
        router.get(API_ROOT + "/renter_fees").respond(200, json={"code": 0, "renter_fees": {"on_demand": {
            "base": {"USD-Blockchain": 5}}}, "creation_fees": {"on_demand": {"USD-Blockchain": 0.1}}})
        create = router.post(CREATE).mock(return_value=OK)
        router.get(ORDERS).mock(side_effect=lambda request: run_order())
        code = main(["up", MODEL, "--untested", "--max-spend", "5", "--backends", "clore"], environ=CLORE_ENV)
        assert code == 0 and create.call_count == 1
        assert not any(call.request.url.host in ("console.vast.ai", "api.runpod.io") for call in router.calls)
        assert not any(call.request.url.path.endswith("/cancel_order") for call in router.calls)
        assert create.calls[0].request.headers["auth"] == "invented-clore-key"
    body = json.loads(create.calls[0].request.content)
    assert body["renting_server"] == 7101 and body["type"] == "on-demand" and body["currency"] == "USD-Blockchain"
    assert body["ports"] == {"22": "tcp"} and body["autossh_entrypoint"] is True
    assert body["ssh_key"] == "ssh-ed25519 invented-public-content" and "ssh_password" not in body and "env" not in body
    assert body["required_price"] == pytest.approx(4.8)
    record = RunStore().all()[0]
    assert record["backend"] == "clore" and record["resource_ids"] == ["9001"]
    assert record["local_cap_primary"] is True and not record.get("watchdog_key_id") and "ssh_key_id" not in record
    raw = RunStore().path(record["run_id"]).read_text()
    assert keys.secret_get(record["run_id"]) not in raw and "invented-clore-key" not in raw
    assert keys.secret_get("watchdog/" + record["run_id"]) is None


def test_the_default_choice_includes_clore_when_a_clore_key_exists_and_clore_is_cheaper(monkeypatch):
    launch_environment(monkeypatch)
    keys.secret_set("accounts/vast", "invented-vast-key")
    keys.secret_set("accounts/clore", "invented-clore-key")
    with respx.mock(assert_all_called=False) as router:
        router.post(VAST_URL).mock(side_effect=FakeVast([vast_row(7101, base=2.0)]))
        router.get(MARKET).mock(side_effect=lambda request: market(4.8))
        router.get(API_ROOT + "/renter_fees").respond(200, json={"code": 0, "renter_fees": {"on_demand": {
            "base": {"USD-Blockchain": 5}}}, "creation_fees": {"on_demand": {"USD-Blockchain": 0.1}}})
        create = router.post(CREATE).mock(return_value=OK)
        router.get(ORDERS).mock(side_effect=lambda request: run_order())
        assert main(["up", MODEL, "--untested", "--max-spend", "5"], environ={}) == 0
        assert create.call_count == 1
    assert RunStore().all()[0]["backend"] == "clore"


def test_the_default_choice_leaves_clore_out_when_there_is_no_clore_key(monkeypatch):
    launch_environment(monkeypatch)
    keys.secret_set("accounts/vast", "invented-vast-key")
    with respx.mock(assert_all_called=False) as router:
        router.post(VAST_URL).mock(side_effect=FakeVast([vast_row(7101)]))
        clore_search = router.get(MARKET).mock(side_effect=lambda request: market(0.2))
        router.post("https://console.vast.ai/api/v0/ssh/").respond(200, json={"success": True, "key": {"id": 7501}})
        router.put("https://console.vast.ai/api/v0/asks/7101/").respond(200, json={"success": True, "new_contract": 7201})
        router.post("https://console.vast.ai/api/v0/auth/apikeys/").respond(200, json={"id": 7401, "key": "invented-token"})
        router.get("https://console.vast.ai/api/v1/instances/").mock(side_effect=lambda request: httpx.Response(
            200, json={"success": True, "next_token": None, "instances": [
                {"id": 7201, "label": "ablit-" + RunStore().all()[0]["run_id"], "actual_status": "running",
                 "ssh_host": "h.example.invalid", "ssh_port": 2201, "host_id": 7301, "dph_total": 1.0, "volume_info": []}]}))
        assert main(["up", MODEL, "--untested", "--max-spend", "5"], environ={}) == 0
        assert clore_search.call_count == 0
    assert RunStore().all()[0]["backend"] == "vast"


def test_a_clore_key_alone_searches_clore_alone_and_with_runpod_it_searches_both(monkeypatch):
    launch_environment(monkeypatch)
    keys.secret_set("accounts/clore", "invented-clore-key")
    keys.secret_set("accounts/runpod", "invented-runpod-key")
    assert up_mod.choose_backends(type("A", (), {"backends": None})(), {}) == ["runpod", "clore"]
    keys.secret_delete("accounts/runpod")
    assert up_mod.choose_backends(type("A", (), {"backends": None})(), {}) == ["clore"]
    keys.secret_set("accounts/vast", "invented-vast-key")
    assert up_mod.choose_backends(type("A", (), {"backends": None})(), {}) == ["vast", "clore"]


def test_a_price_change_between_quote_and_rent_cancels_a_clore_run(monkeypatch, capsys):
    launch_environment(monkeypatch)
    state = {"calls": 0}

    def drifting(request):
        state["calls"] += 1
        return market(4.8 if state["calls"] == 1 else 9.0)  # the search comes first, then the requote

    with respx.mock(assert_all_called=False) as router:
        router.get(MARKET).mock(side_effect=drifting)
        router.get(API_ROOT + "/renter_fees").respond(200, json={"code": 0, "renter_fees": {"on_demand": {
            "base": {"USD-Blockchain": 5}}}, "creation_fees": {"on_demand": {"USD-Blockchain": 0.1}}})
        create = router.post(CREATE).mock(return_value=OK)
        code = main(["up", MODEL, "--untested", "--max-spend", "5", "--backends", "clore"], environ=CLORE_ENV)
    assert code == 5 and create.call_count == 0
    assert "price changed" in capsys.readouterr().out


def test_a_lost_create_answer_exits_7_and_never_sends_a_second_create(monkeypatch, capsys):
    launch_environment(monkeypatch)
    with respx.mock(assert_all_called=False) as router:
        router.get(MARKET).mock(side_effect=lambda request: market())
        router.get(API_ROOT + "/renter_fees").respond(200, json={"code": 0, "renter_fees": {"on_demand": {
            "base": {"USD-Blockchain": 5}}}, "creation_fees": {"on_demand": {"USD-Blockchain": 0.1}}})
        create = router.post(CREATE).mock(side_effect=httpx.ReadTimeout("lost"))
        router.get(ORDERS).mock(return_value=orders())
        code = main(["up", MODEL, "--untested", "--max-spend", "5", "--backends", "clore"], environ=CLORE_ENV)
    assert code == 7 and create.call_count == 1


def saved_clore_run(store, **changes):
    from test_run_commands import saved_run
    import test_vast_lifecycle
    original = test_vast_lifecycle.OFFER
    test_vast_lifecycle.OFFER = OFFER
    try:
        data = saved_run(store)
    finally:
        test_vast_lifecycle.OFFER = original
    data.update({"resource_ids": ["9001"], "ssh_host": "n1.example.invalid", "ssh_port": 10022,
                 "watchdog_key_id": None, "watchdog_key_sent": False, "host_id": "7101", **changes})
    store.save(data)
    return data


def test_down_cancels_the_order_and_confirms_billing_stopped(capsys):
    store = RunStore()
    run = saved_clore_run(store)
    assert run["backend"] == "clore" and run["local_cap_primary"] is True
    keys.secret_set(run["run_id"], "invented-endpoint-key")
    states = iter([orders(order(9001)), orders()])  # found by the reconcile, then gone after the cancel
    with respx.mock as router:
        router.get(ORDERS).mock(side_effect=lambda request: next(states))
        cancel = router.post(CANCEL).mock(return_value=OK)
        assert main(["down", run["run_id"]], environ=CLORE_ENV) == 0
        assert cancel.call_count == 1 and json.loads(cancel.calls[0].request.content) == {"id": 9001}
    assert "Billing stopped" in capsys.readouterr().out
    assert store.load(run["run_id"])["state"] == "billing_stopped_confirmed"
    assert keys.secret_get(run["run_id"]) is None


def test_down_exits_7_while_the_order_is_still_listed(capsys):
    store = RunStore()
    run = saved_clore_run(store)
    with respx.mock as router:
        router.get(ORDERS).mock(return_value=orders(order(9001)))
        router.post(CANCEL).mock(return_value=OK)
        assert main(["down", run["run_id"]], environ=CLORE_ENV) == 7
    out = capsys.readouterr().out
    assert "Orders are still in the user's order list" in out and "clore.ai" in out
    assert store.load(run["run_id"])["state"] != "billing_stopped_confirmed"


def test_status_shows_the_saved_state_without_a_call_when_another_command_holds_the_lock(capsys):
    store = RunStore()
    run = saved_clore_run(store)
    with store.lock(run["run_id"]):
        with respx.mock(assert_all_called=False) as router:
            orders_route = router.get(ORDERS).mock(return_value=orders(order(9001)))
            assert main(["status", run["run_id"], "--json"], environ=CLORE_ENV) == 0
            assert orders_route.call_count == 0
    captured = capsys.readouterr()
    assert "another command holds the run lock" in captured.err
    assert json.loads(captured.out)["runs"][0]["backend"] == "clore"


def test_status_reports_an_absent_order_and_cleans_up(capsys):
    store = RunStore()
    run = saved_clore_run(store)
    keys.secret_set(run["run_id"], "invented-endpoint-key")
    monkey_probe = SSH.probe
    SSH.probe = lambda self: {}
    try:
        with respx.mock as router:
            router.get(ORDERS).mock(return_value=orders())
            router.post(CANCEL).mock(return_value=OK)
            assert main(["status", run["run_id"], "--json"], environ=CLORE_ENV) == 0
    finally:
        SSH.probe = monkey_probe
    assert store.load(run["run_id"])["state"] == "billing_stopped_confirmed"


def monitor_run(monkeypatch, store, run, listed, cancel):
    """Run the real monitor loop once, with the start deadline already passed."""
    monkeypatch.setattr(monitor_mod.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(CloreBackend, "_sleep", staticmethod(lambda seconds: None))
    monkeypatch.setattr(CloreBackend, "request_interval", 0)
    driver = CloreBackend(httpx.Client(), store=store)
    with respx.mock as router:
        router.get(ORDERS).mock(side_effect=listed)
        router.post(CANCEL).mock(side_effect=cancel)
        return monitor_mod.monitor(store, run, driver, keys.credentials("clore", CLORE_ENV), want_ready=True)


def test_a_clore_run_with_no_endpoint_at_the_deadline_is_cancelled_and_exits_6(monkeypatch):
    store = RunStore()
    run = saved_clore_run(store, state="creating", ssh_host=None, ssh_port=None, ready=False,
                          started_at=time.time() - monitor_mod.START_DEADLINE_SECONDS - 60)
    keys.secret_set(run["run_id"], "invented-endpoint-key")
    # The order has no forwarded SSH port yet. After the cancel it is gone from the list.
    answers = iter([orders(order(9001, pub_cluster=None, tcp_ports=None)), orders(), orders()])
    assert monitor_run(monkeypatch, store, run, lambda request: next(answers), lambda request: OK) == 6
    assert store.load(run["run_id"])["state"] == "billing_stopped_confirmed"
    assert "7101" in store.denied_hosts("clore")
    assert "7101" not in store.denied_hosts("vast")


def test_a_clore_run_whose_cancel_is_not_confirmed_exits_7(monkeypatch):
    store = RunStore()
    run = saved_clore_run(store, state="creating", ssh_host=None, ssh_port=None, ready=False,
                          started_at=time.time() - monitor_mod.START_DEADLINE_SECONDS - 60)
    keys.secret_set(run["run_id"], "invented-endpoint-key")
    code = monitor_run(monkeypatch, store, run, lambda request: orders(order(9001, pub_cluster=None, tcp_ports=None)),
                       lambda request: httpx.Response(500, json={"code": 1}))
    assert code == 7
    assert store.load(run["run_id"])["state"] != "billing_stopped_confirmed"


def test_a_clore_run_ended_by_the_marketplace_before_an_endpoint_exits_6(monkeypatch):
    store = RunStore()
    run = saved_clore_run(store, state="creating", ssh_host=None, ssh_port=None, ready=False)
    keys.secret_set(run["run_id"], "invented-endpoint-key")
    answers = iter([orders(), orders(), orders()])  # the order is absent from the first look
    assert monitor_run(monkeypatch, store, run, lambda request: next(answers), lambda request: OK) == 6


def test_the_clore_run_uses_the_local_check_even_when_the_heartbeat_is_fresh():
    run = saved_clore_run(RunStore())
    now = time.time()
    run.update(marketplace_state="running", heartbeat={"at": now, "spend": 0, "action": "continue"},
               spend_estimate_usd=run["cap_usd"])
    assert local_stop_needed(run, now) is True


def test_plan_adds_the_local_check_note_when_clore_is_chosen_and_not_otherwise(capsys):
    with respx.mock(assert_all_called=False) as router:
        router.post(VAST_URL).mock(side_effect=FakeVast([vast_row(7101, base=2.0)]))
        router.get(MARKET).mock(side_effect=lambda request: market(4.8))
        assert main(["plan", MODEL, "--max-spend", "5", "--backends", "vast,clore"], environ={}) == 0
    out = capsys.readouterr().out
    assert "Chosen offer" in out and "On Clore, a key cannot be limited to one machine" in out
    with respx.mock(assert_all_called=False) as router:
        router.post(VAST_URL).mock(side_effect=FakeVast([vast_row(7101, base=0.1)]))
        router.get(MARKET).mock(side_effect=lambda request: market(48.0))
        assert main(["plan", MODEL, "--max-spend", "5", "--backends", "vast,clore"], environ={}) == 0
    assert "On Clore, a key cannot be limited" not in capsys.readouterr().out


def test_plan_drops_a_clore_server_that_lacks_a_needed_field_and_says_why():
    bad = server(7102)
    bad["specs"].pop("disk")
    no_price = server(7103)
    no_price["price"]["on_demand"]["USD-Blockchain"] = None
    with respx.mock as router:
        router.get(MARKET).mock(return_value=httpx.Response(200, json={"code": 0, "servers": [server(7101), bad, no_price]}))
        found = CloreBackend(httpx.Client()).search(
            _request(), None)
    assert [o.offer_id for o in found.offers] == ["7101"]
    assert "2 servers missing a field ablit needs" in found.note


def test_a_server_with_a_field_of_the_wrong_type_still_stops_the_search():
    from ablit.backends.errors import UnknownFieldType
    bad = server(7102)
    bad["specs"]["gpuram"] = "24 GB"
    with respx.mock as router:
        router.get(MARKET).mock(return_value=httpx.Response(200, json={"code": 0, "servers": [bad]}))
        with pytest.raises(UnknownFieldType):
            CloreBackend(httpx.Client()).search(_request(), None)


def _entry():
    from ablit.catalog import load_catalog, resolve
    return resolve(load_catalog().entries, MODEL)


def _request():
    from ablit.select.request import request_from_entry
    return request_from_entry(_entry(), 10.0)


def test_the_watchdog_on_a_clore_machine_never_calls_an_api(tmp_path, monkeypatch):
    def fail(*args, **kwargs):
        raise AssertionError("no network call is allowed")

    monkeypatch.setattr(watchdog.urllib.request, "urlopen", fail)
    config = tmp_path / "run.json"
    config.write_text(json.dumps({"backend": "clore", "instance_id": "9001"}))
    with pytest.raises(RuntimeError, match="no key limited to one order"):
        watchdog.destroy_instance("cap", str(config))


def test_up_refuses_an_unknown_backend_name(monkeypatch, capsys):
    launch_environment(monkeypatch)
    with respx.mock:
        assert main(["up", MODEL, "--untested", "--max-spend", "5", "--backends", "vast,lambda"], environ=CLORE_ENV) == 2
    assert "clore" in capsys.readouterr().err


def test_up_refuses_an_offer_with_no_allocated_disk_before_anything_is_rented(monkeypatch, capsys):
    launch_environment(monkeypatch)
    plan = up_mod.run_plan

    def no_disk(*args, **kwargs):
        from dataclasses import replace
        report = plan(*args, **kwargs)
        return replace(report, ranked=[replace(r, offer=replace(r.offer, allocated_disk_gb=None)) for r in report.ranked])

    monkeypatch.setattr(up_mod, "run_plan", no_disk)
    with respx.mock(assert_all_called=False) as router:
        router.get(MARKET).mock(side_effect=lambda request: market())
        router.get(API_ROOT + "/renter_fees").respond(200, json={"code": 0, "renter_fees": {"on_demand": {
            "base": {"USD-Blockchain": 5}}}, "creation_fees": {"on_demand": {"USD-Blockchain": 0.1}}})
        create = router.post(CREATE).mock(return_value=OK)
        code = main(["up", MODEL, "--untested", "--max-spend", "5", "--backends", "clore"], environ=CLORE_ENV)
    assert code == 5 and create.call_count == 0
    assert RunStore().all() == []
    assert "no allocated disk size" in capsys.readouterr().out
