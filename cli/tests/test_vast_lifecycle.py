"""Lifecycle HTTP tests use invented IDs, keys and prices only."""

import json
from dataclasses import asdict

import httpx
import pytest
import respx

from ablit.backends import Allocation, BootSpec, Creds, Inventory
from ablit.backends.errors import CreateUncertain, ResponseNotRecognized
from ablit.backends.vast import VastBackend
from ablit.runs import RunStore
from helpers import make_offer

ROOT = "https://console.vast.ai"
KEY = Creds("invented-account-key")
RUN = "invented-run"
OFFER = make_offer("7101", allocated_disk_gb=20)
BOOT = BootSpec("example/image@sha256:" + "a" * 64, "mkdir -p /ablit", 20)
ALLOC = Allocation(RUN, "vast", ["7201"], 1.0)


def instance(instance_id=7201, label="ablit-" + RUN, **overrides):
    return dict(id=instance_id, label=label, actual_status="running", ssh_host="ssh.example.invalid",
                ssh_port=2201, host_id=7301, dph_total=1.0, volume_info=[], **overrides)


def page(rows, next_token=None):
    return httpx.Response(200, json={"success": True, "instances": rows, "next_token": next_token})


@pytest.fixture
def driver(tmp_path):
    store = RunStore(tmp_path)
    store.save({"run_id": RUN, "state": "creating", "model_id": "invented-model", "resource_ids": []})
    return VastBackend(httpx.Client(), store=store)


def test_create_body_and_durable_id(driver):
    with respx.mock as router:
        route = router.put(ROOT + "/api/v0/asks/7101/").respond(200, json={"success": True, "new_contract": 7201})
        found = driver.create(OFFER, BOOT, RUN, KEY)
    body = json.loads(route.calls[0].request.content)
    assert body == {"image": BOOT.image, "disk": 20, "label": "ablit-" + RUN, "runtype": "ssh",
                    "target_state": "running", "onstart": BOOT.onstart, "cancel_unavail": True}
    assert KEY.api_key not in json.dumps(body)
    assert found.resource_ids == ["7201"]
    assert driver.store.load(RUN)["create_sent"] is True
    assert driver.store.load(RUN)["resource_ids"] == ["7201"]


def test_fault_drops_response_and_a_new_driver_recovers_without_second_create(driver, monkeypatch):
    monkeypatch.setenv("ABLIT_FAULT", "drop-create-response")
    with respx.mock as router:
        create = router.put(ROOT + "/api/v0/asks/7101/").respond(200, json={"success": True, "new_contract": 7201})
        with pytest.raises(CreateUncertain):
            driver.create(OFFER, BOOT, RUN, KEY)
        assert driver.store.load(RUN).get("resource_ids") == []
        router.get(ROOT + "/api/v1/instances/").mock(return_value=page([instance()]))
        restarted = VastBackend(httpx.Client(), store=driver.store)
        found = restarted.create(OFFER, BOOT, RUN, KEY)
        assert found.resource_ids == ["7201"]
        assert create.call_count == 1


def test_timeout_is_journaled_even_if_label_is_not_yet_visible(driver):
    with respx.mock as router:
        create = router.put(ROOT + "/api/v0/asks/7101/").mock(side_effect=httpx.ReadTimeout("invented"))
        with pytest.raises(httpx.ReadTimeout):
            driver.create(OFFER, BOOT, RUN, KEY)
        router.get(ROOT + "/api/v1/instances/").mock(return_value=page([]))
        with pytest.raises(CreateUncertain):
            driver.create(OFFER, BOOT, RUN, KEY)
        assert create.call_count == 1


def test_reconcile_and_billing_check_read_every_page(driver):
    with respx.mock as router:
        route = router.get(ROOT + "/api/v1/instances/").mock(side_effect=[
            page([instance(7202, "unrelated")], "invented-cursor"), page([instance()]),
            page([instance(7202, "unrelated")], "invented-cursor"), page([instance()])])
        assert driver.reconcile(RUN, KEY).resource_ids == ["7201"]
        assert not driver.verify_billing_stopped(Inventory(RUN, "vast", ["7201"]), KEY).stopped
        assert route.calls[1].request.url.params["after_token"] == "invented-cursor"


def test_billing_stopped_has_no_invented_final_cost(driver):
    with respx.mock as router:
        router.get(ROOT + "/api/v1/instances/").mock(return_value=page([]))
        evidence = driver.verify_billing_stopped(Inventory(RUN, "vast", ["7201"]), KEY)
    assert evidence.stopped and evidence.final_cost_usd is None


def test_observe_uses_documented_fields_and_preserves_unknown_cost(driver):
    with respx.mock as router:
        router.get(ROOT + "/api/v1/instances/").mock(return_value=page([instance()]))
        observation = driver.observe(ALLOC, KEY)
    assert observation.state == "running" and observation.spend_usd is None
    assert observation.hourly_rate == 1.0
    assert observation.allocation.ssh_host == "ssh.example.invalid"


def test_scoped_key_body_and_revocation(driver):
    with respx.mock as router:
        create = router.post(ROOT + "/api/v0/auth/apikeys/").respond(200, json={"id": 7401, "key": "invented-scoped-token"})
        key = driver.watchdog_credential(ALLOC, KEY)
        assert json.loads(create.calls[0].request.content) == {
            "name": "ablit-" + RUN,
            "permissions": {"api": {"instance_write": {"api.instance": {"DELETE": {"constraints": {"id": {"eq": 7201}}}}}}}}
        assert key.key_id == "7401" and key.instance_id == "7201"
        assert key.token not in repr(key)
        router.delete(ROOT + "/api/v0/auth/apikeys/7401/").respond(200, json="Successfully Deleted API Key")
        driver.delete_watchdog_key(key.key_id, KEY)


def test_ssh_registration_and_deletion(driver):
    with respx.mock as router:
        public = "ssh-ed25519 invented-public-value"
        register = router.post(ROOT + "/api/v0/ssh/").respond(200, json={"success": True, "key": {"id": 7501}})
        assert driver.register_ssh_key(public, KEY) == "7501"
        assert json.loads(register.calls[0].request.content) == {"ssh_key": public}
        router.delete(ROOT + "/api/v0/ssh/7501/").respond(200, json={"success": True})
        driver.delete_ssh_key("7501", KEY)


@pytest.mark.parametrize("payload", [[], {}, {"success": True, "instances": []},
                                    {"success": True, "instances": [False], "next_token": None}])
def test_unknown_inventory_shape_is_refused(driver, payload):
    with respx.mock as router:
        router.get(ROOT + "/api/v1/instances/").respond(200, json=payload)
        with pytest.raises(ResponseNotRecognized):
            driver.reconcile(RUN, KEY)


def test_unknown_volume_shape_is_refused(driver):
    row = instance()
    row["volume_info"] = [{"undocumented": 1}]
    with respx.mock as router:
        router.get(ROOT + "/api/v1/instances/").mock(return_value=page([row]))
        with pytest.raises(ResponseNotRecognized, match="volume_info"):
            driver.reconcile(RUN, KEY)


def test_repeated_ssh_key_delete_accepts_only_documented_missing_code(driver):
    with respx.mock as router:
        route = router.delete(ROOT + "/api/v0/ssh/7501/").respond(400, json={"success": False, "error": "no_ssh_key"})
        driver.delete_ssh_key("7501", KEY)
        route.respond(400, json={"success": False, "error": "invented-other-error"})
        from ablit.backends.errors import RequestRejected
        with pytest.raises(RequestRejected):
            driver.delete_ssh_key("7501", KEY)


def test_deny_list_is_sent_in_the_search_request(driver):
    from test_vast import REQ, OPTS
    from market_fixtures import VAST_URL, FakeVast, vast_row
    fake = FakeVast([vast_row()])
    driver.denied_host_ids = {"7301"}
    with respx.mock as router:
        router.post(VAST_URL).mock(side_effect=fake)
        driver.search(REQ, KEY, OPTS)
    assert fake.bodies[0]["host_id"] == {"notin": [7301]}


def test_explicit_create_rejection_is_distinct_from_lost_response(driver):
    from ablit.backends.errors import RequestRejected
    with respx.mock as router:
        route = router.put(ROOT + "/api/v0/asks/7101/").respond(404, json={"success": False, "error": "not_found"})
        with pytest.raises(RequestRejected):
            driver.create(OFFER, BOOT, RUN, KEY)
    assert driver.store.load(RUN)["create_rejected"] is True
    assert route.call_count == 1


def test_pinned_cuda_requirement_is_sent_to_search(driver):
    from ablit.catalog import load_catalog, resolve
    from ablit.select.request import request_from_entry
    from ablit.backends.vast import build_body
    from ablit.select import PlanOptions
    entry = resolve(load_catalog().entries, "llama31-8b-abliterated-q4km")
    body = build_body(request_from_entry(entry), PlanOptions())
    assert body["cpu_arch"] == {"eq": "amd64"}
    assert body["cuda_max_good"] == {"gte": 12.8}


def test_inventory_reads_are_paced_without_repeating_mutations(driver, monkeypatch):
    import ablit.backends.vast as vast
    driver.inventory_interval = 1.1
    monkeypatch.setattr(vast.time, "monotonic", lambda: 10.0)
    pauses = []
    monkeypatch.setattr(vast.time, "sleep", pauses.append)
    with respx.mock as router:
        router.get(ROOT + "/api/v1/instances/").mock(return_value=page([]))
        driver.reconcile(RUN, KEY)
        driver.reconcile(RUN, KEY)
    assert pauses == [1.1]


def test_every_vast_path_ends_with_a_slash_and_redirects_are_refused():
    from ablit.backends import Creds
    from ablit.backends.errors import RequestRejected
    driver = VastBackend(httpx.Client())
    with pytest.raises(ValueError):
        driver._request("GET", "/api/v1/instances", Creds("invented-account-key"))
    with respx.mock as router:
        router.get(ROOT + "/api/v1/instances/").respond(301, headers={"Location": ROOT + "/elsewhere/"})
        with pytest.raises(RequestRejected):
            driver.test_credentials(Creds("invented-account-key"))


def test_reads_retry_after_a_rate_limit_and_creates_do_not():
    from ablit.backends.errors import RequestRejected
    driver = VastBackend(httpx.Client())
    waits = []
    driver._sleep = waits.append
    with respx.mock as router:
        router.get(ROOT + "/api/v1/instances/").mock(side_effect=[
            httpx.Response(429, headers={"Retry-After": "3"}), httpx.Response(503),
            httpx.Response(200, json={"success": True, "instances": [], "next_token": None})])
        put = router.put(ROOT + "/api/v0/asks/1/").respond(429)
        driver.test_credentials(Creds("invented-account-key"))
        assert waits == [3.0, 4.0]
        with pytest.raises(RequestRejected):
            driver._request("PUT", "/api/v0/asks/1/", Creds("invented-account-key"), body={})
        assert put.call_count == 1
