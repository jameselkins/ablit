"""RunPod rental tests use invented IDs, keys and prices only.

These tests show how the driver handles the calls in RunPod's v2 OpenAPI schema
(https://api.runpod.io/v2/openapi.json). Nothing here shows that renting works on a
real RunPod machine, because none has been rented.
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
from ablit.backends.runpod import RUNPOD_START, RunPodBackend, offer_id_for
from ablit.runs import RunStore
from helpers import make_offer

ROOT = "https://api.runpod.io"
PODS = ROOT + "/v2/pods"
KEY = Creds("invented-runpod-key")
RUN = "invented-run"
IMAGE = "example/image@sha256:" + "a" * 64
SSH_KEY = "ssh-ed25519 AAAAinvented+key/value== invented-run"
BOOT = BootSpec(IMAGE, RUNPOD_START, 20, ssh_public_key=SSH_KEY)
GPU = "NVIDIA GeForce RTX 4090"
OFFER = make_offer(offer_id_for(GPU, 1, "secure"), "runpod", 0.52, sticker_hourly=0.5, host_ram_gb=100,
                   allocated_disk_gb=20, verified=True)
COMMUNITY_OFFER = make_offer(offer_id_for(GPU, 1, "any"), "runpod", 0.42, sticker_hourly=0.4, host_ram_gb=100,
                             allocated_disk_gb=20)
ALLOC = Allocation(RUN, "runpod", ["pod1"], 0.52)


def pod(pod_id="pod1", name="ablit-" + RUN, status="RUNNING", **overrides):
    """One invented pod, in the shape the OpenAPI schema describes."""
    row = {
        "id": pod_id, "name": name, "status": status, "image": IMAGE, "disk": 20, "cloud": "SECURE",
        "gpu": {"id": GPU, "count": 1, "vcpuCount": 16, "memory": 120}, "cost": 0.52,
        "ssh": {"proxy": {"host": "ssh.example.invalid", "port": 22, "username": "opaque", "command": "ssh opaque@x"},
                "direct": {"host": "203.0.113.7", "port": 34446, "username": "root", "command": "ssh root@x"}},
        "mounts": {}, "ports": ["22/tcp"], "env": {},
    }
    row.update(overrides)
    return row


def listing(*rows, next_cursor=None):
    return httpx.Response(200, json={"pods": list(rows), "pagination": {"nextCursor": next_cursor,
                                                                        "hasNextPage": next_cursor is not None}})


def created(**overrides):
    return httpx.Response(201, json=pod(**overrides))


@pytest.fixture
def driver(tmp_path):
    store = RunStore(tmp_path)
    store.save({"run_id": RUN, "state": "creating", "model_id": "invented-model", "resource_ids": []})
    backend = RunPodBackend(httpx.Client(), store=store)
    backend._sleep = lambda seconds: None
    return backend


# --- create --------------------------------------------------------------------

def test_create_body_and_durable_id(driver):
    with respx.mock as router:
        route = router.post(PODS).mock(return_value=created())
        found = driver.create(OFFER, BOOT, RUN, KEY)
    request = route.calls[0].request
    body = json.loads(request.content)
    assert body == {
        "name": "ablit-" + RUN, "image": IMAGE, "gpu": {"id": GPU, "count": 1, "minRamPerGpu": 100},
        "cloud": "SECURE", "disk": 20, "ports": ["22/tcp"], "env": {"PUBLIC_KEY": SSH_KEY},
        "entrypoint": ["/bin/bash", "-c"], "cmd": [RUNPOD_START],
    }
    assert "mounts" not in body  # no persistent volume, so a stopped pod has no volume to bill
    assert "terminateAfter" not in body and "startSsh" not in body
    assert request.headers["Authorization"] == "Bearer invented-runpod-key"
    assert KEY.api_key not in json.dumps(body)
    assert found.resource_ids == ["pod1"]
    record = driver.store.load(RUN)
    assert record["create_sent"] is True and record["resource_ids"] == ["pod1"]


def test_an_offer_that_beat_the_secure_price_is_created_in_the_community_cloud(driver):
    with respx.mock as router:
        route = router.post(PODS).mock(return_value=created(cloud="COMMUNITY", cost=0.42))
        driver.create(COMMUNITY_OFFER, BOOT, RUN, KEY)
    assert json.loads(route.calls[0].request.content)["cloud"] == "COMMUNITY"


def test_the_start_script_writes_the_key_and_runs_sshd_without_a_password():
    assert "$PUBLIC_KEY" in RUNPOD_START and "authorized_keys" in RUNPOD_START
    assert "PasswordAuthentication=no" in RUNPOD_START and "sshd -D" in RUNPOD_START
    assert "RUNPOD_API_KEY" not in RUNPOD_START


def test_the_attempt_is_journaled_before_the_request_leaves(driver):
    seen = {}

    def handler(request):
        seen["create_sent"] = driver.store.load(RUN).get("create_sent")
        return created()

    with respx.mock as router:
        router.post(PODS).mock(side_effect=handler)
        driver.create(OFFER, BOOT, RUN, KEY)
    assert seen["create_sent"] is True


def test_fault_drops_response_and_a_new_driver_recovers_by_name_without_second_create(driver, monkeypatch):
    monkeypatch.setenv("ABLIT_FAULT", "drop-create-response")
    with respx.mock as router:
        create = router.post(PODS).mock(return_value=created())
        with pytest.raises(CreateUncertain):
            driver.create(OFFER, BOOT, RUN, KEY)
        assert driver.store.load(RUN).get("resource_ids") == []
        router.get(PODS).mock(return_value=listing(pod("other", name="someone-elses-pod"), pod()))
        restarted = RunPodBackend(httpx.Client(), store=driver.store)
        found = restarted.create(OFFER, BOOT, RUN, KEY)
        assert found.resource_ids == ["pod1"]
        assert create.call_count == 1


def test_a_timeout_is_journaled_and_a_second_create_is_never_sent(driver):
    with respx.mock as router:
        create = router.post(PODS).mock(side_effect=httpx.ReadTimeout("invented"))
        with pytest.raises(httpx.ReadTimeout):
            driver.create(OFFER, BOOT, RUN, KEY)
        router.get(PODS).mock(return_value=listing())
        with pytest.raises(CreateUncertain):
            driver.create(OFFER, BOOT, RUN, KEY)
        assert create.call_count == 1
    assert driver.store.load(RUN).get("create_rejected") is None


@pytest.mark.parametrize("status", [400, 401, 402, 403, 404, 422])
def test_an_explicit_refusal_is_marked_and_is_never_retried(driver, status):
    with respx.mock as router:
        route = router.post(PODS).mock(return_value=httpx.Response(
            status, json={"title": "x", "status": status, "detail": "no capacity for this GPU"}))
        with pytest.raises((RequestRejected, CredentialsRejected)):
            driver.create(OFFER, BOOT, RUN, KEY)
    assert driver.store.load(RUN)["create_rejected"] is True
    assert route.call_count == 1


@pytest.mark.parametrize("status", [429, 500, 502])
def test_an_unclear_failure_is_not_marked_as_refused_and_is_never_retried(driver, status):
    with respx.mock as router:
        route = router.post(PODS).mock(return_value=httpx.Response(status))
        with pytest.raises(RequestRejected):
            driver.create(OFFER, BOOT, RUN, KEY)
    record = driver.store.load(RUN)
    assert record["create_sent"] is True and record.get("create_rejected") is None
    assert route.call_count == 1


@pytest.mark.parametrize("key", [None, "", "ssh-ed25519 bad$key", "ssh-ed25519 two\nlines", "k" * 1025])
def test_a_key_the_driver_refuses_stops_the_create_before_anything_is_sent(driver, key):
    with respx.mock as router:
        route = router.post(PODS).mock(return_value=created())
        with pytest.raises(ValueError, match="SSH public key"):
            driver.create(OFFER, BootSpec(IMAGE, "x", 20, ssh_public_key=key), RUN, KEY)
    assert route.call_count == 0 and "create_sent" not in driver.store.load(RUN)


def test_an_image_without_a_digest_or_with_env_values_is_refused(driver):
    with respx.mock as router:
        route = router.post(PODS).mock(return_value=created())
        with pytest.raises(ResponseNotRecognized, match="digest"):
            driver.create(OFFER, BootSpec("example/image:latest", "x", 20, ssh_public_key=SSH_KEY), RUN, KEY)
        with pytest.raises(ResponseNotRecognized, match="SSH"):
            driver.create(OFFER, BootSpec(IMAGE, "x", 20, {"TOKEN": "invented"}, SSH_KEY), RUN, KEY)
    assert route.call_count == 0


@pytest.mark.parametrize("change,word", [
    ({"gpu": {"id": "NVIDIA A40", "count": 1, "memory": 120}}, "GPU type"),
    ({"gpu": {"id": GPU, "count": 1, "memory": 40}}, "RAM"),
    ({"disk": 10}, "disk"),
    ({"cloud": "COMMUNITY"}, "cloud"),
    ({"cost": 0.90}, "hourly cost"),
])
def test_a_created_pod_that_differs_from_the_quote_is_refused_after_its_id_is_saved(driver, change, word):
    with respx.mock as router:
        router.post(PODS).mock(return_value=created(**change))
        with pytest.raises(ResponseNotRecognized, match=word):
            driver.create(OFFER, BOOT, RUN, KEY)
    # The ID is saved first, so `down` finds and destroys the pod.
    assert driver.store.load(RUN)["resource_ids"] == ["pod1"]


def test_a_pod_a_little_dearer_than_the_quote_inside_the_margin_is_accepted(driver):
    with respx.mock as router:
        router.post(PODS).mock(return_value=created(cost=0.53))
        assert driver.create(OFFER, BOOT, RUN, KEY).resource_ids == ["pod1"]


def test_an_unknown_pod_shape_or_status_is_refused(driver):
    with respx.mock as router:
        router.post(PODS).mock(return_value=httpx.Response(201, json=pod(status="SLEEPING")))
        with pytest.raises(UnknownFieldType):
            driver.create(OFFER, BOOT, RUN, KEY)


# --- reconcile, observe -------------------------------------------------------

def test_reconcile_matches_the_run_name_and_ignores_terminated_and_other_pods(driver):
    with respx.mock as router:
        router.get(PODS).mock(return_value=listing(
            pod("pod1"), pod("pod2", name="ablit-another"), pod("pod3", status="TERMINATED"), pod("pod4", name=None)))
        assert driver.reconcile(RUN, KEY).resource_ids == ["pod1"]


def test_the_list_follows_every_page(driver):
    with respx.mock as router:
        route = router.get(PODS).mock(side_effect=[listing(pod("pod9", name="x"), next_cursor="c1"), listing(pod())])
        assert driver.reconcile(RUN, KEY).resource_ids == ["pod1"]
    assert route.calls[1].request.url.params["cursor"] == "c1"


@pytest.mark.parametrize("payload", [
    [], {"pods": None}, {"pods": []}, {"pods": [False], "pagination": {"nextCursor": None, "hasNextPage": False}},
])
def test_unknown_list_shapes_are_refused(driver, payload):
    with respx.mock as router:
        router.get(PODS).respond(200, json=payload)
        with pytest.raises((ResponseNotRecognized, UnknownFieldType)):
            driver.reconcile(RUN, KEY)


def test_a_repeated_cursor_is_refused(driver):
    with respx.mock as router:
        router.get(PODS).mock(return_value=listing(pod("x", name="y"), next_cursor="same"))
        with pytest.raises(ResponseNotRecognized, match="cursor"):
            driver.reconcile(RUN, KEY)


def test_observe_reads_the_direct_ssh_endpoint_and_the_cost(driver):
    with respx.mock as router:
        router.get(PODS).mock(return_value=listing(pod(), pod("pod2", name="other")))
        observation = driver.observe(ALLOC, KEY)
    assert observation.state == "running" and observation.hourly_rate == 0.52
    found = observation.allocation
    assert (found.ssh_host, found.ssh_port) == ("203.0.113.7", 34446)
    assert "ssh.example.invalid" != found.ssh_host  # the proxy cannot forward ports, so it is never used


@pytest.mark.parametrize("change", [
    {"status": "PROVISIONING", "ssh": {"proxy": None, "direct": None}},
    {"status": "RUNNING", "ssh": {"proxy": None, "direct": None}},
])
def test_a_pod_with_no_direct_ssh_yet_is_starting(driver, change):
    with respx.mock as router:
        router.get(PODS).mock(return_value=listing(pod(**change)))
        observation = driver.observe(ALLOC, KEY)
    assert observation.state == "starting" and observation.allocation.ssh_port is None


@pytest.mark.parametrize("status,state", [("EXITED", "exited"), ("ERROR", "exited"), ("STARTING", "starting")])
def test_other_statuses_map_to_the_states_the_run_loop_knows(driver, status, state):
    with respx.mock as router:
        router.get(PODS).mock(return_value=listing(pod(status=status)))
        assert driver.observe(ALLOC, KEY).state == state


@pytest.mark.parametrize("rows", [[], [pod(status="TERMINATED")]])
def test_a_missing_or_terminated_pod_is_absent(driver, rows):
    with respx.mock as router:
        router.get(PODS).mock(return_value=listing(*rows))
        assert driver.observe(ALLOC, KEY).state == "absent"


@pytest.mark.parametrize("ssh", [
    "text", {"direct": "text"}, {"direct": {"host": "bad host!", "port": 22}}, {"direct": {"host": "h.example", "port": 99999}},
])
def test_observe_refuses_unknown_ssh_shapes(driver, ssh):
    with respx.mock as router:
        router.get(PODS).mock(return_value=listing(pod(ssh=ssh)))
        with pytest.raises(UnknownFieldType):
            driver.observe(ALLOC, KEY)


# --- destroy and billing ------------------------------------------------------

def test_destroy_terminates_each_pod_and_never_stops_one(driver):
    with respx.mock as router:
        route = router.delete(url__regex=r"https://api\.runpod\.io/v2/pods/.+").mock(return_value=httpx.Response(204))
        result = driver.destroy(Inventory(RUN, "runpod", ["pod1", "pod2"]), KEY)
        assert router.calls.call_count == 2
    assert [call.request.url.path for call in route.calls] == ["/v2/pods/pod1", "/v2/pods/pod2"]
    assert all(call.request.method == "DELETE" for call in route.calls)
    assert all("action" not in call.request.url.path for call in route.calls)  # a stopped pod still bills for its volume
    assert route.calls[0].request.headers["Authorization"] == "Bearer invented-runpod-key"
    assert result.requested and result.resource_ids_terminated == ["pod1", "pod2"]


def test_destroy_treats_a_missing_pod_as_done_and_refuses_a_bad_key_or_id(driver):
    with respx.mock as router:
        route = router.delete(PODS + "/pod1").mock(return_value=httpx.Response(404))
        assert driver.destroy(Inventory(RUN, "runpod", ["pod1"]), KEY).resource_ids_terminated == ["pod1"]
        route.mock(return_value=httpx.Response(403))
        with pytest.raises(CredentialsRejected):
            driver.destroy(Inventory(RUN, "runpod", ["pod1"]), KEY)
        route.mock(return_value=httpx.Response(409, json={"title": "x", "status": 409, "detail": "cluster pod"}))
        with pytest.raises(RequestRejected, match="cluster pod"):
            driver.destroy(Inventory(RUN, "runpod", ["pod1"]), KEY)
    with pytest.raises(ValueError):
        driver.destroy(Inventory(RUN, "runpod", ["../account"]), KEY)


def test_destroy_retries_a_rate_limit_and_a_server_error_because_a_delete_can_be_repeated(driver):
    waits = []
    driver._sleep = waits.append
    with respx.mock as router:
        route = router.delete(PODS + "/pod1").mock(side_effect=[
            httpx.Response(429, headers={"Retry-After": "7"}), httpx.Response(503), httpx.Response(204)])
        driver.destroy(Inventory(RUN, "runpod", ["pod1"]), KEY)
    assert route.call_count == 3 and waits[0] == 7.0


def test_billing_is_stopped_when_the_pod_is_gone_or_terminated(driver):
    with respx.mock as router:
        router.get(PODS).mock(return_value=listing(pod("pod2", name="other"), pod(status="TERMINATED")))
        evidence = driver.verify_billing_stopped(Inventory(RUN, "runpod", ["pod1"]), KEY)
    assert evidence.stopped and evidence.final_cost_usd is None


def test_a_stopped_pod_still_counts_as_billing(driver):
    with respx.mock as router:
        router.get(PODS).mock(return_value=listing(pod(status="EXITED", cost=0.0)))
        assert driver.verify_billing_stopped(Inventory(RUN, "runpod", ["pod1"]), KEY).still_billable == ["pod1"]


def test_billing_check_also_counts_a_pod_found_only_by_name(driver):
    with respx.mock as router:
        router.get(PODS).mock(return_value=listing(pod("pod7")))
        evidence = driver.verify_billing_stopped(Inventory(RUN, "runpod", []), KEY)
    assert evidence.still_billable == ["pod7"]


# --- account and the spend cap -------------------------------------------------

def test_the_key_test_is_one_read_only_call(driver):
    with respx.mock as router:
        route = router.get(PODS).mock(return_value=listing())
        driver.test_credentials(KEY)
    assert route.call_count == 1 and route.calls[0].request.url.params["limit"] == "1"
    assert route.calls[0].request.headers["Authorization"] == "Bearer invented-runpod-key"


def test_a_refused_key_is_a_credential_error(driver):
    with respx.mock as router:
        router.get(PODS).respond(403)
        with pytest.raises(CredentialsRejected):
            driver.test_credentials(KEY)


def test_there_is_no_scoped_watchdog_credential_and_the_driver_says_so(driver):
    assert driver.watchdog_credential(ALLOC, KEY) is None
    capabilities = driver.capabilities()
    assert capabilities.search is True
    assert capabilities.scoped_watchdog_credential is False
    assert capabilities.marketplace_deadline is False  # the v2 create request has no deadline field
    assert capabilities.rent is True  # one paid round trip passed on 2026-10-22


def test_the_driver_sends_no_write_call_other_than_create_and_delete(driver):
    with respx.mock as router:
        router.post(PODS).mock(return_value=created())
        router.get(PODS).mock(return_value=listing(pod()))
        router.delete(PODS + "/pod1").mock(return_value=httpx.Response(204))
        driver.create(OFFER, BOOT, RUN, KEY)
        driver.observe(ALLOC, KEY)
        driver.destroy(Inventory(RUN, "runpod", ["pod1"]), KEY)
        driver.verify_billing_stopped(Inventory(RUN, "runpod", ["pod1"]), KEY)
        methods = sorted({call.request.method for call in router.calls})
    assert methods == ["DELETE", "GET", "POST"]
