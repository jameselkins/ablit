"""Clore lifecycle tests use invented IDs, keys and prices only.

These tests show how the driver handles the documented calls. Nothing here shows
that renting works on a real Clore machine, because none has been rented.
"""

import json
import time

import httpx
import pytest
import respx

from ablit.backends import Allocation, BootSpec, Creds, Inventory
from ablit.backends.clore import API_ROOT, CloreBackend, CloreRejected
from ablit.backends.errors import (
    CreateUncertain,
    CredentialsRejected,
    RequestRejected,
    ResponseNotRecognized,
    UnknownFieldType,
    UnknownUnit,
)
from ablit.runs import RunStore
from helpers import make_offer

KEY = Creds("invented-clore-key")
RUN = "invented-run"
IMAGE = "example/image@sha256:" + "a" * 64
SSH_KEY = "ssh-ed25519 AAAAinvented+key/value== ablit@example"
BOOT = BootSpec(IMAGE, "mkdir -p /ablit", 20, ssh_public_key=SSH_KEY)
OFFER = make_offer("7101", "clore", 0.21, sticker_hourly=0.2, fixed_fees=0.10, host_id="7101")
ALLOC = Allocation(RUN, "clore", ["9001"], 0.21)

CREATE = API_ROOT + "/create_order"
ORDERS = API_ROOT + "/my_orders"
CANCEL = API_ROOT + "/cancel_order"


def order(order_id=9001, server=7101, image=IMAGE, ct=None, **overrides):
    """One invented row of the order list, in the shape the API reference shows."""
    row = {
        "id": order_id, "si": server, "image": image, "currency": "USD-Blockchain",
        "price": 4.8, "fee": 5, "creation_fee": 0.1, "mrl": 259200, "spend": 0.0,
        "gpu_count": 1, "ct": int(time.time()) if ct is None else ct, "spot": False, "expired": False,
        "pub_cluster": ["n1.example.invalid"], "tcp_ports": ["22:10022"], "http_port": None,
    }
    row.update(overrides)
    return row


def orders(*rows):
    return httpx.Response(200, json={"code": 0, "orders": list(rows)})


OK = httpx.Response(200, json={"code": 0})


@pytest.fixture
def driver(tmp_path):
    store = RunStore(tmp_path)
    store.save({"run_id": RUN, "state": "creating", "model_id": "invented-model", "resource_ids": []})
    return CloreBackend(httpx.Client(), store=store)


# --- create ------------------------------------------------------------------

def test_create_body_and_durable_id(driver):
    with respx.mock as router:
        route = router.post(CREATE).mock(return_value=OK)
        router.get(ORDERS).mock(return_value=orders(order()))
        found = driver.create(OFFER, BOOT, RUN, KEY)
    request = route.calls[0].request
    body = json.loads(request.content)
    assert body == {
        "currency": "USD-Blockchain", "image": IMAGE, "renting_server": 7101, "type": "on-demand",
        "ports": {"22": "tcp"}, "ssh_key": SSH_KEY, "autossh_entrypoint": True,
        "command": BOOT.onstart, "required_price": 4.8,
    }
    assert "gpu_count" not in body  # whole servers only
    assert request.headers["auth"] == "invented-clore-key"
    assert KEY.api_key not in json.dumps(body)
    assert found.resource_ids == ["9001"] and found.host_id == "7101"
    record = driver.store.load(RUN)
    assert record["create_sent"] is True and record["resource_ids"] == ["9001"]
    assert record["clore_create"]["server_id"] == 7101 and record["clore_create"]["image"] == IMAGE


def test_the_attempt_is_journaled_before_the_request_leaves(driver):
    seen = {}

    def handler(request):
        record = driver.store.load(RUN)
        seen.update(create_sent=record.get("create_sent"), journal=record.get("clore_create"))
        return OK

    with respx.mock as router:
        router.post(CREATE).mock(side_effect=handler)
        router.get(ORDERS).mock(return_value=orders(order()))
        driver.create(OFFER, BOOT, RUN, KEY)
    assert seen["create_sent"] is True and seen["journal"]["server_id"] == 7101


def test_fault_drops_response_and_a_new_driver_recovers_without_second_create(driver, monkeypatch):
    monkeypatch.setenv("ABLIT_FAULT", "drop-create-response")
    with respx.mock as router:
        create = router.post(CREATE).mock(return_value=OK)
        with pytest.raises(CreateUncertain):
            driver.create(OFFER, BOOT, RUN, KEY)
        assert driver.store.load(RUN).get("resource_ids") == []
        router.get(ORDERS).mock(return_value=orders(order(9002, server=7999), order()))
        restarted = CloreBackend(httpx.Client(), store=driver.store)
        found = restarted.create(OFFER, BOOT, RUN, KEY)
        assert found.resource_ids == ["9001"]
        assert create.call_count == 1


def test_a_timeout_is_journaled_and_the_order_is_not_yet_visible(driver):
    with respx.mock as router:
        create = router.post(CREATE).mock(side_effect=httpx.ReadTimeout("invented"))
        with pytest.raises(httpx.ReadTimeout):
            driver.create(OFFER, BOOT, RUN, KEY)
        router.get(ORDERS).mock(return_value=orders())
        with pytest.raises(CreateUncertain):
            driver.create(OFFER, BOOT, RUN, KEY)
        assert create.call_count == 1
    assert driver.store.load(RUN).get("create_rejected") is None


def test_an_order_that_shows_up_late_is_found_without_a_second_create(driver):
    with respx.mock as router:
        create = router.post(CREATE).mock(return_value=OK)
        listing = router.get(ORDERS).mock(side_effect=[orders(), orders(), orders(), orders(order())])
        with pytest.raises(CreateUncertain):
            driver.create(OFFER, BOOT, RUN, KEY)
        assert listing.call_count == 3
        found = driver.create(OFFER, BOOT, RUN, KEY)
        assert found.resource_ids == ["9001"] and create.call_count == 1


def test_the_order_list_is_read_again_before_the_driver_gives_up(driver):
    with respx.mock as router:
        router.post(CREATE).mock(return_value=OK)
        listing = router.get(ORDERS).mock(side_effect=[orders(), orders(order())])
        found = driver.create(OFFER, BOOT, RUN, KEY)
    assert found.resource_ids == ["9001"] and listing.call_count == 2


def test_an_order_with_other_values_is_not_taken_for_this_run(driver):
    old = order(9003, ct=int(time.time()) - 86400)
    other_server = order(9004, server=7555)
    spot = order(9005, spot=True)
    with respx.mock as router:
        router.post(CREATE).mock(return_value=OK)
        router.get(ORDERS).mock(return_value=orders(old, other_server, spot))
        with pytest.raises(CreateUncertain):
            driver.create(OFFER, BOOT, RUN, KEY)


def test_a_matching_order_with_a_different_image_is_not_adopted(driver):
    with respx.mock as router:
        router.post(CREATE).mock(return_value=OK)
        router.get(ORDERS).mock(return_value=orders(order(image="example/other@sha256:" + "b" * 64)))
        with pytest.raises(ResponseNotRecognized, match="image"):
            driver.create(OFFER, BOOT, RUN, KEY)
    assert driver.store.load(RUN)["resource_ids"] == []


def test_two_matching_orders_are_not_guessed_between(driver):
    with respx.mock as router:
        router.post(CREATE).mock(return_value=OK)
        router.get(ORDERS).mock(return_value=orders(order(9001), order(9002)))
        with pytest.raises(ResponseNotRecognized, match="more than one"):
            driver.create(OFFER, BOOT, RUN, KEY)


@pytest.mark.parametrize("response", [
    httpx.Response(200, json={"code": 6, "error": "server-already-rented"}),
    httpx.Response(200, json={"code": 2}),
    httpx.Response(400, json={"code": 2}),
    httpx.Response(401, json={"code": 3}),
    httpx.Response(404, json={"code": 4}),
])
def test_an_explicit_refusal_is_marked_and_is_distinct_from_a_lost_response(driver, response):
    with respx.mock as router:
        route = router.post(CREATE).mock(return_value=response)
        with pytest.raises(RequestRejected):
            driver.create(OFFER, BOOT, RUN, KEY)
    assert driver.store.load(RUN)["create_rejected"] is True
    assert route.call_count == 1


@pytest.mark.parametrize("response", [
    httpx.Response(200, json={"code": 1}),
    httpx.Response(200, json={"code": 5}),
    httpx.Response(429),
    httpx.Response(500),
])
def test_an_unclear_failure_is_not_marked_as_refused_and_is_never_retried(driver, response):
    with respx.mock as router:
        route = router.post(CREATE).mock(return_value=response)
        with pytest.raises(RequestRejected):
            driver.create(OFFER, BOOT, RUN, KEY)
    record = driver.store.load(RUN)
    assert record["create_sent"] is True and record.get("create_rejected") is None
    assert route.call_count == 1


def test_a_second_create_after_a_failed_one_does_not_send_again(driver):
    with respx.mock as router:
        route = router.post(CREATE).mock(return_value=httpx.Response(500))
        with pytest.raises(RequestRejected):
            driver.create(OFFER, BOOT, RUN, KEY)
        router.get(ORDERS).mock(return_value=orders())
        with pytest.raises(CreateUncertain):
            driver.create(OFFER, BOOT, RUN, KEY)
        assert route.call_count == 1


@pytest.mark.parametrize("key", [None, "", "ssh-ed25519 bad$key", "k" * 3073])
def test_a_key_the_api_would_refuse_stops_the_create_before_anything_is_sent(driver, key):
    boot = BootSpec(IMAGE, "x", 20, ssh_public_key=key)
    with respx.mock as router:
        route = router.post(CREATE).mock(return_value=OK)
        with pytest.raises(ValueError, match="SSH public key"):
            driver.create(OFFER, boot, RUN, KEY)
    assert route.call_count == 0 and "create_sent" not in driver.store.load(RUN)


def test_an_image_without_a_digest_or_with_env_values_is_refused(driver):
    with respx.mock as router:
        route = router.post(CREATE).mock(return_value=OK)
        with pytest.raises(ResponseNotRecognized, match="digest"):
            driver.create(OFFER, BootSpec("example/image:latest", "x", 20, ssh_public_key=SSH_KEY), RUN, KEY)
        with pytest.raises(ResponseNotRecognized, match="SSH"):
            driver.create(OFFER, BootSpec(IMAGE, "x", 20, {"TOKEN": "invented"}, SSH_KEY), RUN, KEY)
    assert route.call_count == 0


def test_an_offer_with_no_listed_price_cannot_be_rented(driver):
    with respx.mock as router:
        route = router.post(CREATE).mock(return_value=OK)
        with pytest.raises(ValueError, match="no listed price"):
            driver.create(make_offer("7101", "clore", 0.2), BOOT, RUN, KEY)
    assert route.call_count == 0


# --- reconcile, observe -------------------------------------------------------

def test_reconcile_uses_saved_ids_and_leaves_look_alike_orders_alone(driver):
    record = driver.store.load(RUN)
    record.update(create_sent=True, resource_ids=["9001"],
                  clore_create={"server_id": 7101, "image": IMAGE, "sent_at": int(time.time())})
    driver.store.save(record)
    with respx.mock as router:
        router.get(ORDERS).mock(return_value=orders(order(9001), order(9002, server=7555), order(9009)))
        assert driver.reconcile(RUN, KEY).resource_ids == ["9001"]


def test_reconcile_finds_the_order_by_the_journal_when_no_id_was_saved(driver):
    record = driver.store.load(RUN)
    record.update(create_sent=True, clore_create={"server_id": 7101, "image": IMAGE, "sent_at": int(time.time())})
    driver.store.save(record)
    with respx.mock as router:
        router.get(ORDERS).mock(return_value=orders(order(9002, server=7555), order(9009)))
        assert driver.reconcile(RUN, KEY).resource_ids == ["9009"]


def test_reconcile_with_no_create_sent_reads_nothing(driver):
    with respx.mock as router:
        route = router.get(ORDERS).mock(return_value=orders(order()))
        assert driver.reconcile(RUN, KEY).resource_ids == []
        assert driver.reconcile("unknown-run", KEY).resource_ids == []
    assert route.call_count == 0


@pytest.mark.parametrize("payload", [[], {"code": 0}, {"code": 0, "orders": [False]}, {"code": 0, "orders": [{"si": 1}]}])
def test_unknown_order_list_shapes_are_refused(driver, payload):
    record = driver.store.load(RUN)
    record["create_sent"] = True
    driver.store.save(record)
    with respx.mock as router:
        router.get(ORDERS).respond(200, json=payload)
        with pytest.raises(ResponseNotRecognized):
            driver.reconcile(RUN, KEY)


def test_observe_uses_documented_fields(driver):
    with respx.mock as router:
        route = router.get(ORDERS).mock(return_value=orders(order(spend=1.25), order(9002, server=7555)))
        observation = driver.observe(ALLOC, KEY)
    assert "return_completed" not in route.calls[0].request.url.params
    assert observation.state == "running" and observation.spend_usd == 1.25
    assert observation.hourly_rate == pytest.approx(4.8 * 1.05 / 24)
    found = observation.allocation
    assert (found.ssh_host, found.ssh_port, found.host_id) == ("n1.example.invalid", 10022, "7101")


def test_an_order_with_no_public_port_yet_is_starting(driver):
    with respx.mock as router:
        router.get(ORDERS).mock(return_value=orders(order(pub_cluster=None, tcp_ports=None)))
        observation = driver.observe(ALLOC, KEY)
    assert observation.state == "starting" and observation.allocation.ssh_port is None


@pytest.mark.parametrize("rows", [[], [order(expired=True)]])
def test_a_missing_or_expired_order_is_absent(driver, rows):
    with respx.mock as router:
        router.get(ORDERS).mock(return_value=orders(*rows))
        assert driver.observe(ALLOC, KEY).state == "absent"


@pytest.mark.parametrize("change,error", [
    ({"currency": "bitcoin"}, UnknownUnit),
    ({"price": "4.8"}, UnknownFieldType),
    ({"fee": "5"}, UnknownFieldType),
    ({"tcp_ports": ["ssh"]}, UnknownFieldType),
    ({"tcp_ports": "22:10022"}, UnknownFieldType),
    ({"pub_cluster": ["bad host!"]}, UnknownFieldType),
    ({"tcp_ports": ["22:99999"]}, UnknownFieldType),
])
def test_observe_refuses_unknown_units_and_shapes(driver, change, error):
    with respx.mock as router:
        router.get(ORDERS).mock(return_value=orders(order(**change)))
        with pytest.raises(error):
            driver.observe(ALLOC, KEY)


# --- destroy and billing ------------------------------------------------------

def test_destroy_cancels_each_order_by_id(driver):
    with respx.mock as router:
        route = router.post(CANCEL).mock(return_value=OK)
        result = driver.destroy(Inventory(RUN, "clore", ["9001", "9002"]), KEY)
    assert [json.loads(call.request.content) for call in route.calls] == [{"id": 9001}, {"id": 9002}]
    assert route.calls[0].request.headers["auth"] == "invented-clore-key"
    assert result.requested and result.resource_ids_terminated == ["9001", "9002"]


def test_destroy_accepts_an_order_that_already_ended_and_nothing_else(driver):
    with respx.mock as router:
        route = router.post(CANCEL).mock(return_value=httpx.Response(200, json={"code": 6, "error": "order_not_active"}))
        assert driver.destroy(Inventory(RUN, "clore", ["9001"]), KEY).resource_ids_terminated == ["9001"]
        route.mock(return_value=httpx.Response(200, json={"code": 6, "error": "you_dont_own_this_order"}))
        with pytest.raises(CloreRejected):
            driver.destroy(Inventory(RUN, "clore", ["9001"]), KEY)
        route.mock(return_value=httpx.Response(401, json={"code": 3}))
        with pytest.raises(CredentialsRejected):
            driver.destroy(Inventory(RUN, "clore", ["9001"]), KEY)


def test_destroy_retries_after_a_rate_limit_because_a_cancel_can_be_repeated(driver):
    driver._sleep = lambda seconds: None
    with respx.mock as router:
        route = router.post(CANCEL).mock(side_effect=[httpx.Response(200, json={"code": 5}), OK])
        driver.destroy(Inventory(RUN, "clore", ["9001"]), KEY)
    assert route.call_count == 2


def test_billing_is_stopped_when_the_order_is_gone_and_no_final_cost_is_invented(driver):
    with respx.mock as router:
        router.get(ORDERS).mock(return_value=orders(order(9002, server=7555)))
        evidence = driver.verify_billing_stopped(Inventory(RUN, "clore", ["9001"]), KEY)
    assert evidence.stopped and evidence.final_cost_usd is None


def test_billing_is_not_stopped_while_the_order_is_listed(driver):
    with respx.mock as router:
        router.get(ORDERS).mock(return_value=orders(order()))
        assert driver.verify_billing_stopped(Inventory(RUN, "clore", ["9001"]), KEY).still_billable == ["9001"]


def test_billing_check_also_counts_an_order_found_only_by_the_journal(driver):
    record = driver.store.load(RUN)
    record.update(create_sent=True, clore_create={"server_id": 7101, "image": IMAGE, "sent_at": int(time.time())})
    driver.store.save(record)
    with respx.mock as router:
        router.get(ORDERS).mock(return_value=orders(order(9050)))
        evidence = driver.verify_billing_stopped(Inventory(RUN, "clore", []), KEY)
    assert evidence.still_billable == ["9050"]


def test_an_expired_order_does_not_count_as_billing(driver):
    with respx.mock as router:
        router.get(ORDERS).mock(return_value=orders(order(expired=True)))
        assert driver.verify_billing_stopped(Inventory(RUN, "clore", ["9001"]), KEY).stopped


# --- the spend cap has no scoped key on Clore ----------------------------------

def test_there_is_no_scoped_watchdog_credential_and_the_driver_says_so(driver):
    assert driver.watchdog_credential(ALLOC, KEY) is None
    capabilities = driver.capabilities()
    assert capabilities.search is True
    assert capabilities.scoped_watchdog_credential is False
    assert capabilities.rent is False  # not true until a rented machine has worked


@pytest.mark.parametrize("missing", [True, False])
def test_observe_adds_the_documented_renter_fee_when_the_order_has_none(driver, missing):
    # A live order list read on 2026-10-26 had no fee field.
    row = order(fee=None)
    if missing:
        del row["fee"]
    with respx.mock as router:
        router.get(ORDERS).mock(return_value=orders(row))
        observation = driver.observe(ALLOC, KEY)
    assert observation.hourly_rate == pytest.approx(4.8 * 1.05 / 24)
