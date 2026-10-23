"""Lium rental tests use invented IDs, keys and prices only.

These tests show how the driver handles the calls in Lium's OpenAPI schema
(https://lium.io/api/openapi.json) and the documented answers. Nothing here shows that renting
works on a real Lium machine, because none has been rented.
"""

import json

import httpx
import pytest
import respx

from ablit.backends import Allocation, BootSpec, Creds, Inventory
from ablit.backends.errors import (
    CreateUncertain,
    CredentialsRejected,
    RequestRejected,
    ResponseNotRecognized,
    UnknownFieldType,
)
from ablit.backends.lium import API_ROOT, LIUM_START, LiumBackend, offer_id_for
from ablit.runs import RunStore
from helpers import make_offer

PODS = API_ROOT + "/pods"
TEMPLATES = API_ROOT + "/templates"
NODE = "11111111-1111-4111-8111-111111111111"
RENT = API_ROOT + f"/executors/{NODE}/rent"
KEY = Creds("invented-lium-key")
RUN = "invented-run"
IMAGE = "example/image@sha256:" + "a" * 64
SSH_KEY = "ssh-ed25519 AAAAinvented+key/value== invented-run"
BOOT = BootSpec(IMAGE, LIUM_START, 20, ssh_public_key=SSH_KEY)
POD = "22222222-2222-4222-8222-222222222222"
TEMPLATE = "33333333-3333-4333-8333-333333333333"
OFFER = make_offer(offer_id_for(NODE, 1), "lium", 0.40, sticker_hourly=0.40, host_ram_gb=128, allocated_disk_gb=1000,
                   verified=True, host_id=NODE)
ALLOC = Allocation(RUN, "lium", [POD], 0.40)


def pod(pod_id=POD, name="ablit-" + RUN, status="RUNNING", **overrides):
    """One invented pod, in the shape of the pod list in the OpenAPI schema."""
    row = {
        "id": pod_id, "pod_name": name, "status": status, "executor_id": NODE, "price": 0.40, "pending_price": None,
        "spend_to_date": 0.05, "ssh_connect_cmd": "ssh root@203.0.113.7 -p 34446", "gpu_name": "RTX 4090",
        "gpu_count": "1", "removal_scheduled_at": None, "termination_hours": 12,
    }
    row.update(overrides)
    return row


def listing(*rows):
    return httpx.Response(200, json=list(rows))


def made(template=TEMPLATE):
    return httpx.Response(200, json={"id": template, "name": "ablit-" + RUN})


def rented(pod_id=POD):
    return httpx.Response(200, json={"success": True, "pod_id": pod_id})


def error(status, code, message="refused"):
    return httpx.Response(status, json={"success": False, "error": {"code": code, "message": message, "hint": "h",
                                                                    "request_id": "r"}, "status_code": status})


@pytest.fixture
def driver(tmp_path):
    store = RunStore(tmp_path)
    store.save({"run_id": RUN, "state": "creating", "model_id": "invented-model", "resource_ids": [], "cap_usd": 5.0})
    backend = LiumBackend(httpx.Client(), store=store)
    backend._sleep = lambda seconds: None
    return backend


def send_create(router, **routes):
    router.post(TEMPLATES).mock(return_value=routes.get("template", made()))
    return router.post(RENT).mock(return_value=routes.get("rent", rented()))


# --- create --------------------------------------------------------------------

def test_create_makes_a_one_time_template_then_rents_once_and_saves_the_id(driver):
    with respx.mock as router:
        rent = send_create(router)
        found = driver.create(OFFER, BOOT, RUN, KEY)
        template = router.calls[0].request
    body = json.loads(template.content)
    assert template.url.path == "/api/templates" and template.headers["X-API-Key"] == "invented-lium-key"
    assert body["docker_image"] == "example/image" and body["docker_image_digest"] == "sha256:" + "a" * 64
    assert body["one_time_template"] is True and body["is_private"] is True and body["internal_ports"] == [22]
    assert body["startup_commands"] == LIUM_START and body["environment"] == {"PUBLIC_KEY": SSH_KEY}
    assert body["name"] == "ablit-" + RUN
    request = rent.calls[0].request
    assert json.loads(request.content) == {
        "pod_name": "ablit-" + RUN, "template_id": TEMPLATE, "user_public_key": SSH_KEY, "gpu_count": 1,
        "termination_hours": 12,  # a $5 cap at $0.40 an hour is 12 whole hours
    }
    assert request.headers["X-API-Key"] == "invented-lium-key" and request.headers["Idempotency-Key"] == "ablit-" + RUN
    assert KEY.api_key not in json.dumps(body)
    assert found.resource_ids == [POD] and found.hourly_rate == 0.40
    record = driver.store.load(RUN)
    assert record["create_sent"] is True and record["resource_ids"] == [POD]
    assert record["lium_create"]["node_id"] == NODE and record["lium_create"]["termination_hours"] == 12


@pytest.mark.parametrize("cap,hours", [(0.2, 1), (0.8, 2), (1000.0, 720)])
def test_the_termination_hours_follow_the_cap_within_lium_limits(driver, cap, hours):
    record = driver.store.load(RUN)
    record["cap_usd"] = cap
    driver.store.save(record)
    with respx.mock as router:
        rent = send_create(router)
        driver.create(OFFER, BOOT, RUN, KEY)
    assert json.loads(rent.calls[0].request.content)["termination_hours"] == hours


def test_a_run_with_no_cap_sends_no_termination_hours(driver):
    driver.store.save({"run_id": RUN, "state": "creating", "model_id": "invented-model", "resource_ids": []})
    with respx.mock as router:
        rent = send_create(router)
        driver.create(OFFER, BOOT, RUN, KEY)
    assert "termination_hours" not in json.loads(rent.calls[0].request.content)


def test_the_start_script_writes_the_key_and_keeps_the_container_running():
    assert "$PUBLIC_KEY" in LIUM_START and "authorized_keys" in LIUM_START
    assert "PasswordAuthentication=no" in LIUM_START and LIUM_START.endswith("exec sleep infinity")
    assert "LIUM_API_KEY" not in LIUM_START


def test_the_attempt_is_journaled_before_the_request_leaves(driver):
    seen = {}

    def handler(request):
        seen["create_sent"] = driver.store.load(RUN).get("create_sent")
        return made()

    with respx.mock as router:
        router.post(TEMPLATES).mock(side_effect=handler)
        router.post(RENT).mock(return_value=rented())
        driver.create(OFFER, BOOT, RUN, KEY)
    assert seen["create_sent"] is True


def test_fault_drops_response_and_a_new_driver_recovers_by_name_without_second_create(driver, monkeypatch):
    monkeypatch.setenv("ABLIT_FAULT", "drop-create-response")
    with respx.mock as router:
        rent = send_create(router)
        with pytest.raises(CreateUncertain):
            driver.create(OFFER, BOOT, RUN, KEY)
        assert driver.store.load(RUN).get("resource_ids") == []
        router.get(PODS).mock(return_value=listing(pod("other", name="someone-elses-pod"), pod()))
        restarted = LiumBackend(httpx.Client(), store=driver.store)
        found = restarted.create(OFFER, BOOT, RUN, KEY)
        assert found.resource_ids == [POD]
        assert rent.call_count == 1


def test_a_timeout_on_the_rent_is_journaled_and_a_second_create_is_never_sent(driver):
    with respx.mock as router:
        router.post(TEMPLATES).mock(return_value=made())
        rent = router.post(RENT).mock(side_effect=httpx.ReadTimeout("invented"))
        with pytest.raises(httpx.ReadTimeout):
            driver.create(OFFER, BOOT, RUN, KEY)
        router.get(PODS).mock(return_value=listing())
        with pytest.raises(CreateUncertain):
            driver.create(OFFER, BOOT, RUN, KEY)
        assert rent.call_count == 1
    assert driver.store.load(RUN).get("create_rejected") is None


@pytest.mark.parametrize("status,code", [
    (400, "node_unavailable"), (400, "node_rent_in_progress"), (401, "unauthorized"), (402, "API_KEY_BUDGET_EXCEEDED"),
    (403, "insufficient_balance"), (404, "node_not_found"), (422, "validation_error"),
    (409, "node_paused_by_provider"), (409, "provider_banned"), (409, "conflict"),
])
def test_an_explicit_refusal_is_marked_dropped_from_templates_and_never_retried(driver, status, code):
    with respx.mock as router:
        route = router.post(RENT).mock(return_value=error(status, code))
        router.post(TEMPLATES).mock(return_value=made())
        gone = router.delete(TEMPLATES + "/" + TEMPLATE).mock(return_value=httpx.Response(204))
        with pytest.raises(RequestRejected):
            driver.create(OFFER, BOOT, RUN, KEY)
    assert driver.store.load(RUN)["create_rejected"] is True
    assert route.call_count == 1 and gone.call_count == 1


@pytest.mark.parametrize("status,code", [
    (409, "idempotency_outcome_unknown"), (409, "idempotency_in_progress"), (429, "rate_limited"),
    (500, "internal_error"), (502, "upstream_unavailable"),
])
def test_an_unclear_failure_is_not_marked_as_refused_and_is_never_retried(driver, status, code):
    with respx.mock as router:
        route = router.post(RENT).mock(return_value=error(status, code))
        router.post(TEMPLATES).mock(return_value=made())
        gone = router.delete(TEMPLATES + "/" + TEMPLATE).mock(return_value=httpx.Response(204))
        with pytest.raises(RequestRejected):
            driver.create(OFFER, BOOT, RUN, KEY)
    record = driver.store.load(RUN)
    assert record["create_sent"] is True and record.get("create_rejected") is None
    assert route.call_count == 1 and gone.call_count == 0


@pytest.mark.parametrize("failure", [
    httpx.Response(500), error(400, "image_not_found"), httpx.Response(200, json={"name": "no id"}),
    httpx.ConnectTimeout("invented"),
])
def test_a_template_failure_sends_no_rent_and_proves_nothing_was_created(driver, failure):
    with respx.mock as router:
        if isinstance(failure, Exception):
            template = router.post(TEMPLATES).mock(side_effect=failure)
        else:
            template = router.post(TEMPLATES).mock(return_value=failure)
        rent = router.post(RENT).mock(return_value=rented())
        with pytest.raises(RequestRejected, match="no pod was rented"):
            driver.create(OFFER, BOOT, RUN, KEY)
    assert rent.call_count == 0 and template.call_count == 1
    assert driver.store.load(RUN)["create_rejected"] is True


def test_a_refused_key_on_the_template_step_is_a_credential_error(driver):
    with respx.mock as router:
        router.post(TEMPLATES).mock(return_value=error(401, "unauthorized"))
        with pytest.raises(CredentialsRejected):
            driver.create(OFFER, BOOT, RUN, KEY)
    assert driver.store.load(RUN)["create_rejected"] is True


@pytest.mark.parametrize("key", [None, "", "ssh-ed25519 bad$key", "ssh-ed25519 two\nlines", "k" * 1025])
def test_a_key_the_driver_refuses_stops_the_create_before_anything_is_sent(driver, key):
    with respx.mock as router:
        template = router.post(TEMPLATES).mock(return_value=made())
        with pytest.raises(ValueError, match="SSH public key"):
            driver.create(OFFER, BootSpec(IMAGE, "x", 20, ssh_public_key=key), RUN, KEY)
    assert template.call_count == 0 and "create_sent" not in driver.store.load(RUN)


def test_an_image_without_a_digest_or_with_env_values_is_refused(driver):
    with respx.mock as router:
        template = router.post(TEMPLATES).mock(return_value=made())
        with pytest.raises(ResponseNotRecognized, match="digest"):
            driver.create(OFFER, BootSpec("example/image:latest", "x", 20, ssh_public_key=SSH_KEY), RUN, KEY)
        with pytest.raises(ResponseNotRecognized, match="SSH"):
            driver.create(OFFER, BootSpec(IMAGE, "x", 20, {"TOKEN": "invented"}, SSH_KEY), RUN, KEY)
    assert template.call_count == 0


@pytest.mark.parametrize("answer", [{"success": True}, {"success": False, "pod_id": POD}, [], {"pod_id": "../x"}])
def test_a_rent_answer_with_no_usable_pod_id_is_refused_and_the_pod_is_found_by_name(driver, answer):
    with respx.mock as router:
        send_create(router, rent=httpx.Response(200, json=answer))
        with pytest.raises(ResponseNotRecognized, match="pod_id"):
            driver.create(OFFER, BOOT, RUN, KEY)
        router.get(PODS).mock(return_value=listing(pod()))
        assert driver.reconcile(RUN, KEY).resource_ids == [POD]
    assert driver.store.load(RUN)["create_sent"] is True


# --- reconcile, observe --------------------------------------------------------

def test_reconcile_matches_the_run_name_and_ignores_other_pods(driver):
    with respx.mock as router:
        router.get(PODS).mock(return_value=listing(
            pod(), pod("pod2", name="ablit-another"), pod("pod3", name=None), pod("pod4", name="not-ablit")))
        assert driver.reconcile(RUN, KEY).resource_ids == [POD]


def test_a_key_with_no_balance_and_no_pods_reads_as_an_empty_list(driver):
    with respx.mock as router:
        router.get(PODS).mock(return_value=error(402, "BALANCE_REQUIRED"))
        assert driver.reconcile(RUN, KEY).resource_ids == []


@pytest.mark.parametrize("payload", [{}, "text", [False], [{"pod_name": "x"}]])
def test_unknown_list_shapes_are_refused(driver, payload):
    with respx.mock as router:
        router.get(PODS).respond(200, json=payload)
        with pytest.raises((ResponseNotRecognized, UnknownFieldType)):
            driver.reconcile(RUN, KEY)


def test_observe_reads_the_ssh_command_the_rate_and_the_spend(driver):
    with respx.mock as router:
        route = router.get(PODS).mock(return_value=listing(pod(), pod("pod2", name="other")))
        observation = driver.observe(ALLOC, KEY)
    assert route.calls[0].request.headers["X-API-Key"] == "invented-lium-key"
    assert observation.state == "running" and observation.hourly_rate == 0.40 and observation.spend_usd == 0.05
    found = observation.allocation
    assert (found.ssh_host, found.ssh_port, found.host_id) == ("203.0.113.7", 34446, NODE)


def test_the_ssh_port_defaults_to_22_and_may_come_first(driver):
    with respx.mock as router:
        router.get(PODS).mock(return_value=listing(pod(ssh_connect_cmd="ssh root@h.example")))
        assert driver.observe(ALLOC, KEY).allocation.ssh_port == 22
        router.get(PODS).mock(return_value=listing(pod(ssh_connect_cmd="ssh -p 2200 root@h.example")))
        assert driver.observe(ALLOC, KEY).allocation.ssh_port == 2200


def test_a_pending_price_raise_counts_in_the_observed_rate(driver):
    with respx.mock as router:
        router.get(PODS).mock(return_value=listing(pod(pending_price=0.55)))
        assert driver.observe(ALLOC, KEY).hourly_rate == 0.55


@pytest.mark.parametrize("change", [
    {"status": "PENDING", "ssh_connect_cmd": ""},
    {"status": "RUNNING", "ssh_connect_cmd": None},
    {"status": "RUNNING", "ssh_connect_cmd": ""},
])
def test_a_pod_with_no_ssh_command_yet_is_starting(driver, change):
    with respx.mock as router:
        router.get(PODS).mock(return_value=listing(pod(**change)))
        observation = driver.observe(ALLOC, KEY)
    assert observation.state == "starting" and observation.allocation.ssh_port is None


@pytest.mark.parametrize("status,state", [
    ("PENDING", "starting"), ("REBOOT_PENDING", "starting"), ("START_PENDING", "starting"),
    ("STOPPED", "exited"), ("FAILED", "exited"), ("CREATION_FAILED", "exited"), ("REBOOT_FAILED", "exited"),
    ("BROKEN", "stopped_by_marketplace"),
])
def test_other_statuses_map_to_the_states_the_run_loop_knows(driver, status, state):
    with respx.mock as router:
        router.get(PODS).mock(return_value=listing(pod(status=status)))
        assert driver.observe(ALLOC, KEY).state == state


def test_a_pod_missing_from_the_list_is_absent(driver):
    with respx.mock as router:
        router.get(PODS).mock(return_value=listing(pod("pod2", name="other")))
        assert driver.observe(ALLOC, KEY).state == "absent"


def test_an_unknown_status_is_refused(driver):
    with respx.mock as router:
        router.get(PODS).mock(return_value=listing(pod(status="SLEEPING")))
        with pytest.raises(UnknownFieldType):
            driver.observe(ALLOC, KEY)


@pytest.mark.parametrize("command", [
    "text", "ssh admin@h.example", "ssh root@bad host!", "ssh root@h.example -p 99999", "ssh -p 1 root@h -p 2",
    "ssh root@h.example; rm -rf /", 7,
])
def test_observe_refuses_unknown_ssh_shapes(driver, command):
    with respx.mock as router:
        router.get(PODS).mock(return_value=listing(pod(ssh_connect_cmd=command)))
        with pytest.raises(UnknownFieldType):
            driver.observe(ALLOC, KEY)


# --- destroy and billing -------------------------------------------------------

def test_destroy_deletes_each_pod_by_its_id(driver):
    with respx.mock as router:
        route = router.delete(url__regex=r"https://lium\.io/api/pods/.+").mock(return_value=httpx.Response(204))
        result = driver.destroy(Inventory(RUN, "lium", [POD, "pod2"]), KEY)
    assert [call.request.url.path for call in route.calls] == [f"/api/pods/{POD}", "/api/pods/pod2"]
    assert all(call.request.method == "DELETE" for call in route.calls)
    assert route.calls[0].request.headers["X-API-Key"] == "invented-lium-key"
    assert result.requested and result.resource_ids_terminated == [POD, "pod2"]


def test_destroy_treats_a_missing_pod_as_done_and_refuses_a_bad_key_or_id(driver):
    with respx.mock as router:
        route = router.delete(PODS + "/" + POD).mock(return_value=httpx.Response(404))
        assert driver.destroy(Inventory(RUN, "lium", [POD]), KEY).resource_ids_terminated == [POD]
        route.mock(return_value=error(401, "unauthorized"))
        with pytest.raises(CredentialsRejected):
            driver.destroy(Inventory(RUN, "lium", [POD]), KEY)
        route.mock(return_value=error(400, "pod_state_conflict", "not in a state that allows this"))
        with pytest.raises(RequestRejected, match="pod_state_conflict"):
            driver.destroy(Inventory(RUN, "lium", [POD]), KEY)
    with pytest.raises(ValueError):
        driver.destroy(Inventory(RUN, "lium", ["../account"]), KEY)


def test_destroy_retries_a_rate_limit_and_a_server_error_because_a_delete_can_be_repeated(driver):
    waits = []
    driver._sleep = waits.append
    with respx.mock as router:
        route = router.delete(PODS + "/" + POD).mock(side_effect=[
            httpx.Response(429, headers={"Retry-After": "7"}), httpx.Response(503), httpx.Response(204)])
        driver.destroy(Inventory(RUN, "lium", [POD]), KEY)
    assert route.call_count == 3 and waits[0] == 7.0


def test_billing_is_stopped_when_the_pod_is_gone_or_listed_as_not_charged(driver):
    with respx.mock as router:
        router.get(PODS).mock(return_value=listing(
            pod("pod2", name="other"), pod(status="DELETING"), pod("pod3", name="ablit-" + RUN, status="BROKEN")))
        evidence = driver.verify_billing_stopped(Inventory(RUN, "lium", [POD, "pod3"]), KEY)
    assert evidence.stopped and evidence.final_cost_usd is None


@pytest.mark.parametrize("status", ["RUNNING", "STOPPED", "PENDING", "REBOOT_FAILED"])
def test_a_pod_that_is_still_listed_as_charged_counts_as_billing(driver, status):
    with respx.mock as router:
        router.get(PODS).mock(return_value=listing(pod(status=status)))
        assert driver.verify_billing_stopped(Inventory(RUN, "lium", [POD]), KEY).still_billable == [POD]


def test_billing_check_also_counts_a_pod_found_only_by_name(driver):
    with respx.mock as router:
        router.get(PODS).mock(return_value=listing(pod("pod7")))
        evidence = driver.verify_billing_stopped(Inventory(RUN, "lium", []), KEY)
    assert evidence.still_billable == ["pod7"]


# --- the spend cap -------------------------------------------------------------

def test_there_is_no_scoped_watchdog_credential_and_the_driver_says_so(driver):
    assert driver.watchdog_credential(ALLOC, KEY) is None
    capabilities = driver.capabilities()
    assert capabilities.search is True
    assert capabilities.scoped_watchdog_credential is False
    assert capabilities.marketplace_deadline is False  # termination_hours is sent but untested
    assert capabilities.rent is False  # not true until a rented machine has worked


def test_the_driver_sends_only_the_calls_it_needs(driver):
    with respx.mock as router:
        send_create(router)
        router.get(PODS).mock(return_value=listing(pod()))
        router.delete(PODS + "/" + POD).mock(return_value=httpx.Response(204))
        driver.create(OFFER, BOOT, RUN, KEY)
        driver.observe(ALLOC, KEY)
        driver.destroy(Inventory(RUN, "lium", [POD]), KEY)
        driver.verify_billing_stopped(Inventory(RUN, "lium", [POD]), KEY)
        calls = sorted({(call.request.method, call.request.url.path.split(POD)[0]) for call in router.calls})
    assert calls == [("DELETE", "/api/pods/"), ("GET", "/api/pods"), ("POST", f"/api/executors/{NODE}/rent"),
                     ("POST", "/api/templates")]
