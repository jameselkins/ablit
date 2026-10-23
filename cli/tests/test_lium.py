"""Lium search, offer mapping, filters, errors and setup, with invented data only.

Every node, price, ID and key below is made up. The shapes follow the public nodes feed
(https://docs.lium.io/developers/public-nodes-feed) and the OpenAPI schema at
https://lium.io/api/openapi.json. Nothing here shows that renting works on a real Lium machine.
"""

from __future__ import annotations

import json
import sys

import httpx
import pytest
import respx

from ablit import credentials as keys
from ablit.backends import Creds, default_backends
from ablit.backends.errors import (
    CredentialsRejected,
    RequestRejected,
    ResponseNotRecognized,
    UnknownFieldType,
    UnknownUnit,
)
from ablit.backends.lium import (
    API_ROOT,
    LiumBackend,
    SkipNode,
    offer_from_node,
    offer_id_for,
    parse_offer_id,
)
from ablit.cli import main
from ablit.runs import RunStore
from helpers import OPTS, REQ

FEED = API_ROOT + "/public/v1/nodes"
KEY = Creds("invented-lium-key")
NODE = "11111111-1111-4111-8111-111111111111"


def node(node_id=NODE, **overrides):
    """One invented feed row in the shape the documentation shows."""
    row = {
        "id": node_id, "gpu_model": "RTX 4090", "gpu_count": 1, "available_gpu_count": 1, "available_now": True,
        "price_per_gpu_hour": 0.40, "price_per_node_hour": 0.40, "min_rentable_gpu_count": 1,
        "gpu_memory_gb": 24, "cpu_count": 32, "ram_gb": 128, "disk_gb": 1000, "cpu_model": "Invented CPU",
        "network_upload_mbps": 500.0, "network_download_mbps": 900.5, "uptime_minutes": 4000,
        "reliability_score": 97.5, "disk_type": "nvme", "tier": "secure", "country": "Sweden",
        "country_code": "SE", "region": "AB", "city": "Invented", "rent_url": "https://lium.io/browse-pods/" + node_id,
        "disk_free_gb": 900, "gpu_driver": "570.1", "cuda_driver": "12.8", "gpu_interconnect": None,
        "last_verified_at": "2026-10-23T00:00:00Z",
    }
    row.update(overrides)
    return row


def feed(*rows):
    return httpx.Response(200, json={"generated_at": "2026-10-23T00:00:00Z", "count": len(rows), "nodes": list(rows)})


@pytest.fixture
def driver():
    backend = LiumBackend(httpx.Client())
    backend._sleep = lambda seconds: None
    return backend


# --- offer mapping -------------------------------------------------------------

def test_the_price_is_dollars_per_gpu_hour_times_the_gpu_count_with_no_fee():
    offer = offer_from_node(node(gpu_count=8, available_gpu_count=8, price_per_gpu_hour=0.55,
                                 price_per_node_hour=4.4, min_rentable_gpu_count=1), 2)
    assert offer.hourly_all_in == pytest.approx(1.10) and offer.sticker_hourly == pytest.approx(1.10)
    assert offer.fixed_fees == 0.0 and offer.bandwidth_cost_per_gb == 0.0


def test_offer_carries_the_hardware_fields_and_the_host_id():
    offer = offer_from_node(node(), 1)
    assert offer.backend == "lium" and offer.offer_id == offer_id_for(NODE, 1) and offer.host_id == NODE
    assert (offer.gpu_model, offer.gpu_count, offer.gpu_vram_gb_each) == ("RTX 4090", 1, 24)
    assert offer.host_ram_gb == 128 and offer.disk_gb == 1000 and offer.allocated_disk_gb == 1000
    assert offer.download_mbps == 900.5 and offer.reliability == pytest.approx(0.975) and offer.region == "SE"
    assert offer.compute_capability == 8.9 and offer.rentable and offer.verified and not offer.interruptible
    assert offer.gpu_fraction == 1.0 and offer.ssh_reachable


def test_a_partial_rental_gets_its_share_of_memory_and_disk():
    offer = offer_from_node(node(gpu_count=4, available_gpu_count=4, price_per_gpu_hour=0.2,
                                 price_per_node_hour=0.8, ram_gb=400, disk_gb=2000), 1)
    assert offer.host_ram_gb == 100 and offer.disk_gb == 500 and offer.allocated_disk_gb == 500


def test_a_spot_node_is_interruptible_and_not_verified():
    offer = offer_from_node(node(tier="spot"), 1)
    assert offer.interruptible and not offer.verified


def test_a_node_with_no_free_gpus_is_listed_but_not_rentable():
    assert not offer_from_node(node(gpu_count=2, available_gpu_count=1, price_per_node_hour=0.8), 2).rentable
    assert not offer_from_node(node(available_now=False, available_gpu_count=0), 1).rentable


@pytest.mark.parametrize("row,count", [
    (node(gpu_count=8, available_gpu_count=8, min_rentable_gpu_count=8, price_per_node_hour=3.2), 1),
    (node(), 2),
])
def test_a_node_that_does_not_rent_as_that_many_gpus_is_skipped(row, count):
    with pytest.raises(SkipNode):
        offer_from_node(row, count)


def test_missing_speed_score_and_country_are_allowed():
    offer = offer_from_node(node(network_download_mbps=None, reliability_score=None, country_code=None), 1)
    assert offer.download_mbps is None and offer.reliability is None and offer.region is None


def test_a_gpu_name_the_table_lacks_has_no_compute_capability():
    assert offer_from_node(node(gpu_model="Invented Accelerator"), 1).compute_capability is None


@pytest.mark.parametrize("change,error", [
    ({"price_per_gpu_hour": "0.4"}, UnknownFieldType),
    ({"price_per_gpu_hour": True}, UnknownFieldType),
    ({"gpu_count": 1.5}, UnknownFieldType),
    ({"tier": "premium"}, UnknownFieldType),
    ({"price_per_node_hour": 9.0}, UnknownUnit),
    ({"reliability_score": 0.97 * 1000}, UnknownUnit),
    ({"min_rentable_gpu_count": 4}, UnknownUnit),
])
def test_unknown_shapes_and_units_raise_typed_errors(change, error):
    with pytest.raises(error):
        offer_from_node(node(**change), 1)


def test_a_row_that_is_not_an_object_is_refused():
    with pytest.raises(UnknownFieldType):
        offer_from_node("text", 1)


def test_offer_ids_round_trip_and_bad_ones_are_refused():
    assert parse_offer_id(offer_id_for(NODE, 3)) == (NODE, 3)
    for bad in ("nope", NODE + "|0", "../x|1"):
        with pytest.raises(ValueError):
            parse_offer_id(bad)


# --- search --------------------------------------------------------------------

def test_search_needs_no_key_and_never_sends_one(driver):
    with respx.mock as router:
        route = router.get(FEED).mock(return_value=feed(node()))
        found = driver.search(REQ, KEY, OPTS)
    assert [o.host_id for o in found.offers] == [NODE] and found.complete and found.backend == "lium"
    assert "x-api-key" not in route.calls[0].request.headers
    assert route.calls[0].request.headers["user-agent"].startswith("ablit/")


def test_search_keeps_every_usable_node_and_counts_what_it_leaves_out(driver):
    rows = [
        node("22222222-2222-4222-8222-222222222222", price_per_gpu_hour=0.3, price_per_node_hour=0.3),
        node("33333333-3333-4333-8333-333333333333", gpu_count=8, available_gpu_count=8, min_rentable_gpu_count=8,
             price_per_node_hour=3.2),
        node("44444444-4444-4444-8444-444444444444", ram_gb=None),
        {"id": "55555555-5555-4555-8555-555555555555", "rent_url": "https://lium.io/x"},
    ]
    with respx.mock as router:
        router.get(FEED).mock(return_value=feed(*rows))
        found = driver.search(REQ, None, OPTS)
    assert [o.hourly_all_in for o in found.offers] == [0.3]
    assert "1 nodes that do not rent as 1 GPUs" in found.note and "2 nodes missing a field ablit needs" in found.note
    assert "billed per second" in found.note


def test_the_deny_list_removes_hosts(driver):
    driver.denied_host_ids = {NODE}
    with respx.mock as router:
        router.get(FEED).mock(return_value=feed(node()))
        found = driver.search(REQ, None, OPTS)
    assert found.offers == [] and "1 nodes on the local deny list" in found.note


def test_a_field_of_the_wrong_type_stops_the_search(driver):
    with respx.mock as router:
        router.get(FEED).mock(return_value=feed(node(price_per_gpu_hour="cheap")))
        with pytest.raises(UnknownFieldType):
            driver.search(REQ, None, OPTS)


@pytest.mark.parametrize("payload", [[], {"nodes": None}, {"count": 0}, "text"])
def test_unknown_feed_answers_are_refused(driver, payload):
    with respx.mock as router:
        router.get(FEED).respond(200, json=payload)
        with pytest.raises(ResponseNotRecognized):
            driver.search(REQ, None, OPTS)


def test_an_error_answer_carries_the_code_and_a_bad_key_is_a_credential_error(driver):
    body = {"success": False, "error": {"code": "rate_limited", "message": "slow down", "hint": "wait"},
            "message": "slow down", "status_code": 429}
    with respx.mock as router:
        router.get(FEED).respond(400, json=body)
        with pytest.raises(RequestRejected, match="rate_limited: slow down"):
            driver.search(REQ, None, OPTS)
        router.get(FEED).respond(401, json={"error": {"code": "unauthorized", "message": "no"}})
        with pytest.raises(CredentialsRejected):
            driver.search(REQ, None, OPTS)


def test_reads_wait_and_retry_after_a_rate_limit_and_a_server_error(driver):
    waits = []
    driver._sleep = waits.append
    with respx.mock as router:
        route = router.get(FEED).mock(side_effect=[
            httpx.Response(429, headers={"Retry-After": "7"}), httpx.Response(503), feed(node())])
        assert len(driver.search(REQ, None, OPTS).offers) == 1
    assert route.call_count == 3 and waits[0] == 7.0


def test_redirects_are_refused(driver):
    with respx.mock as router:
        router.get(FEED).respond(302, headers={"Location": "https://elsewhere.invalid/"})
        with pytest.raises(RequestRejected, match="redirected"):
            driver.search(REQ, None, OPTS)


# --- requote -------------------------------------------------------------------

def test_requote_reads_the_feed_again_and_returns_the_current_offer(driver):
    offer = offer_from_node(node(), 1)
    with respx.mock as router:
        router.get(FEED).mock(return_value=feed(node(price_per_gpu_hour=0.45, price_per_node_hour=0.45)))
        again = driver.requote(offer, KEY)
    assert again is not None and again.hourly_all_in == pytest.approx(0.45)


@pytest.mark.parametrize("rows", [
    [], [node("66666666-6666-4666-8666-666666666666")], [node(available_now=False, available_gpu_count=0)],
])
def test_requote_returns_none_when_the_node_is_gone_or_taken(driver, rows):
    with respx.mock as router:
        router.get(FEED).mock(return_value=feed(*rows))
        assert driver.requote(offer_from_node(node(), 1), KEY) is None


# --- the plan command ----------------------------------------------------------

def test_plan_with_lium_only_prices_the_offer_and_adds_the_local_check_note(capsys):
    with respx.mock as router:
        router.get(FEED).mock(return_value=feed(node(price_per_gpu_hour=0.5, price_per_node_hour=0.5)))
        code = main(["plan", "llama31-8b-abliterated-q4km", "--backends", "lium", "--json"],
                    backends={"lium": LiumBackend(httpx.Client())}, environ={})
    out = json.loads(capsys.readouterr().out)
    assert code == 0
    chosen = out["chosen"]
    assert chosen["backend"] == "lium" and chosen["offer_id"] == offer_id_for(NODE, 1)
    assert chosen["hourly_all_in_usd"] == pytest.approx(0.5) and chosen["one_time_fees_usd"] == 0
    assert chosen["verified"] is True
    assert "referral" not in json.dumps(out).lower()


def test_plan_text_names_the_lium_cap_note(capsys):
    with respx.mock as router:
        router.get(FEED).mock(return_value=feed(node()))
        code = main(["plan", "llama31-8b-abliterated-q4km", "--max-spend", "5", "--backends", "lium"],
                    backends={"lium": LiumBackend(httpx.Client())}, environ={})
    assert code == 0 and "On Lium, a key made with your key cannot be limited to one machine" in capsys.readouterr().out


def test_a_default_plan_searches_lium_with_no_key_set():
    assert "lium" in default_backends()


def test_trust_verified_keeps_secure_nodes_and_drops_spot_nodes(driver):
    from ablit.select import PlanOptions, apply_filter_chain
    with respx.mock as router:
        router.get(FEED).mock(return_value=feed(
            node(), node("77777777-7777-4777-8777-777777777777", tier="spot")))
        offers = driver.search(REQ, None, OPTS).offers
    kept = apply_filter_chain(offers, REQ, PlanOptions(trust="verified", spot=True)).kept
    assert [o.host_id for o in kept] == [NODE]


# --- setup ---------------------------------------------------------------------

ME = API_ROOT + "/users/me"


def _tty(monkeypatch, answers, key="invented-setup-key"):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)
    queue = iter(answers)
    monkeypatch.setattr("builtins.input", lambda prompt: next(queue))
    monkeypatch.setattr(keys.getpass, "getpass", lambda prompt: key)


def test_setup_lium_tests_the_key_with_one_read_only_call_and_saves_it(monkeypatch, capsys):
    _tty(monkeypatch, ["yes", "yes"])
    with respx.mock as router:
        route = router.get(ME).mock(return_value=httpx.Response(200, json={"id": "invented", "balance": 1.0}))
        assert main(["setup", "lium"], environ={}) == 0
        assert route.call_count == 1 and route.calls[0].request.method == "GET"
        assert route.calls[0].request.headers["X-API-Key"] == "invented-setup-key"
        assert "authorization" not in route.calls[0].request.headers
    assert keys.secret_get("accounts/lium") == "invented-setup-key"
    assert "invented-setup-key" not in capsys.readouterr().out


def test_setup_lium_can_keep_the_key_in_this_process_only(monkeypatch, capsys):
    _tty(monkeypatch, ["yes", "no"], key="invented-session-key")
    with respx.mock as router:
        router.get(ME).mock(return_value=httpx.Response(200, json={}))
        assert keys.setup_lium(LiumBackend(httpx.Client()), RunStore(), False, {}) == 0
    assert keys.secret_get("accounts/lium") is None and keys.SESSION["lium"] == "invented-session-key"
    assert "LIUM_API_KEY" in capsys.readouterr().out


def test_setup_lium_exits_4_when_the_key_is_rejected(monkeypatch, capsys):
    _tty(monkeypatch, ["yes", "yes"])
    with respx.mock as router:
        router.get(ME).mock(return_value=httpx.Response(401, json={"error": {"code": "unauthorized", "message": "x"}}))
        assert main(["setup", "lium"], environ={}) == 4
    assert keys.secret_get("accounts/lium") is None


def test_setup_lium_reports_an_unrecognized_answer_as_a_failed_test(monkeypatch, capsys):
    _tty(monkeypatch, ["yes", "yes"])
    with respx.mock as router:
        router.get(ME).mock(return_value=httpx.Response(200, json=[1]))
        assert main(["setup", "lium"], environ={}) == 4
    assert "key test failed" in capsys.readouterr().err
    assert keys.secret_get("accounts/lium") is None


def test_no_account_flow_shows_the_direct_link_and_no_paid_link(monkeypatch, capsys):
    _tty(monkeypatch, ["no", "2"])
    with respx.mock:
        assert keys.setup_lium(LiumBackend(httpx.Client()), RunStore(), False, {}) == 0
    out = capsys.readouterr().out
    assert "https://lium.io/" in out and "Sign up directly" in out and "paid link" not in out and "ref=" not in out


def test_the_environment_key_wins_over_the_keyring_and_a_missing_key_names_the_variable():
    keys.secret_set("accounts/lium", "invented-keyring-key")
    assert keys.credentials("lium", {"LIUM_API_KEY": "invented-env-key"}).api_key == "invented-env-key"
    assert keys.credentials("lium", {}).api_key == "invented-keyring-key"
    keys.secret_delete("accounts/lium")
    with pytest.raises(keys.CredentialError, match="LIUM_API_KEY"):
        keys.credentials("lium", {})
