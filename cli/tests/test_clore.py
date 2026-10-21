"""Clore search, price conversion, filters, errors and setup, with invented data only.

Every server, price, ID and key below is made up. The shapes follow
https://docs.clore.ai/for-hosts/api, and the fields that page does not describe
(price.usd, reliability, cuda_version) follow what the live marketplace sent when
the driver was written, with none of its values.
"""

from __future__ import annotations

import copy
import json
import sys

import httpx
import pytest
import respx

from ablit import credentials as keys
from ablit.backends import Creds, default_backends
from ablit.backends.clore import (
    API_ROOT,
    CREATION_FEE_USD,
    RENTER_BASE_FEE_PERCENT,
    CloreBackend,
    CloreRejected,
    SkipServer,
    offer_from_row,
)
from ablit.backends.errors import (
    CredentialsRejected,
    RequestRejected,
    ResponseNotRecognized,
    UnknownFieldType,
    UnknownUnit,
)
from ablit.cli import main
from ablit.runs import RunStore
from helpers import OPTS, REQ

MARKET = API_ROOT + "/marketplace"
KEY = Creds("invented-clore-key")


def server(server_id=1001, gpu="1x NVIDIA GeForce RTX 4090", usd=4.8, **overrides):
    """One invented marketplace row in the shape the live answer has."""
    row = {
        "id": server_id,
        "owner": 5001,
        "mrl": 72,
        "price": {
            "on_demand": {"bitcoin": 0.0001, "CLORE-Blockchain": 11.0, "USD-Blockchain": usd},
            "spot": {"bitcoin": 0.00005, "CLORE-Blockchain": 5.0, "USD-Blockchain": usd / 2},
            "usd": {"on_demand_usd": usd, "spot_usd": usd / 2},
        },
        "rented": False,
        "specs": {
            "gpu": gpu,
            "gpuram": 24,
            "ram": 128,
            "disk": "NVME 1000GB",
            "net": {"down": 800.5, "up": 400.0, "cc": "se"},
        },
        "reliability": 0.97,
        "cuda_version": "13.0",
        "partial_gpu_rental": False,
    }
    for key, value in overrides.items():
        row[key] = value
    return row


def with_specs(row, **changes):
    row = copy.deepcopy(row)
    row["specs"].update(changes)
    return row


def answer(*rows):
    return httpx.Response(200, json={"code": 0, "servers": list(rows)})


@pytest.fixture
def driver():
    return CloreBackend(httpx.Client())


# --- price conversion and fees ----------------------------------------------

def test_price_is_dollars_per_day_and_the_renter_fee_is_five_percent():
    offer = offer_from_row(server(usd=4.8))
    assert RENTER_BASE_FEE_PERCENT == 5.0
    assert offer.sticker_hourly == pytest.approx(0.2)
    assert offer.hourly_all_in == pytest.approx(0.2 * 1.05)
    assert offer.fixed_fees == CREATION_FEE_USD == 0.10
    assert offer.bandwidth_cost_per_gb == 0.0


def test_a_fee_from_the_account_changes_only_the_fee():
    offer = offer_from_row(server(usd=2.4), fee_percent=2.5, creation_fee_usd=0.2)
    assert offer.hourly_all_in == pytest.approx(0.1 * 1.025)
    assert offer.fixed_fees == 0.2


def test_only_the_usd_price_is_used_and_the_coin_prices_are_ignored():
    row = server(usd=7.2)
    row["price"]["on_demand"]["bitcoin"] = 999999.0
    assert offer_from_row(row).sticker_hourly == pytest.approx(0.3)


def test_offer_carries_the_documented_hardware_fields():
    offer = offer_from_row(server(gpu="2x NVIDIA GeForce RTX 3090"))
    assert offer.backend == "clore" and offer.offer_id == "1001" and offer.host_id == "1001"
    assert (offer.gpu_model, offer.gpu_count, offer.gpu_vram_gb_each) == ("RTX 3090", 2, 24.0)
    assert (offer.host_ram_gb, offer.disk_gb, offer.allocated_disk_gb) == (128.0, 1000.0, 1000.0)
    assert offer.download_mbps == 800.5 and offer.region == "SE"
    assert offer.remaining_hours == 72.0
    assert offer.compute_capability == 8.6 and offer.gpu_vendor == "nvidia"
    assert offer.reliability == 0.97
    assert offer.verified is False and offer.interruptible is False and offer.rentable is True


def test_a_gpu_name_the_table_lacks_has_no_compute_capability():
    assert offer_from_row(server(gpu="1x NVIDIA Graphics Device")).compute_capability is None


def test_empty_country_and_missing_speed_are_allowed():
    row = server()
    row["specs"]["net"] = {"down": 0, "up": 0, "cc": ""}
    offer = offer_from_row(row)
    assert offer.region is None and offer.download_mbps == 0.0
    row["specs"]["net"] = {}
    assert offer_from_row(row).download_mbps is None


# --- rows that are not offers ------------------------------------------------

@pytest.mark.parametrize("row,reason", [
    (server(gpu="4x Mixed GPUs"), "mixed-GPU rigs"),
    (server(gpu="1x Intel Arc"), "servers whose GPU vendor the driver does not know"),
])
def test_rows_the_driver_cannot_use_are_skipped_with_a_reason(row, reason):
    with pytest.raises(SkipServer) as skipped:
        offer_from_row(row)
    assert skipped.value.reason == reason


def test_a_server_with_no_usd_price_is_skipped():
    row = server()
    del row["price"]["on_demand"]["USD-Blockchain"]
    with pytest.raises(SkipServer, match="no price in USD"):
        offer_from_row(row)


def test_cuda_floor_is_applied_in_the_driver_and_a_missing_version_fails_it():
    offer_from_row(server(cuda_version="12.8"), cuda_min=12.8)
    with pytest.raises(SkipServer):
        offer_from_row(server(cuda_version="12.4"), cuda_min=12.8)
    row = server()
    del row["cuda_version"]
    with pytest.raises(SkipServer):
        offer_from_row(row, cuda_min=12.8)
    assert offer_from_row(row).rentable is True  # no floor, no problem


def test_rented_and_partly_rented_servers_are_not_rentable_as_a_whole():
    assert offer_from_row(server(rented=True)).rentable is False
    shared = {"total_gpus": 4, "available_gpus": 3, "free_indices": [0, 1, 2]}
    assert offer_from_row(server(gpu="4x NVIDIA GeForce RTX 3090", partial_gpu_rental=shared)).rentable is False
    free = {"total_gpus": 4, "available_gpus": 4, "free_indices": [0, 1, 2, 3]}
    assert offer_from_row(server(gpu="4x NVIDIA GeForce RTX 3090", partial_gpu_rental=free)).rentable is True


# --- unknown shapes and units are refused -------------------------------------

@pytest.mark.parametrize("make,error", [
    (lambda r: r.update(price={"on_demand": {"USD-Blockchain": 0.01}}), UnknownUnit),  # under the 0.1 dollar floor
    (lambda r: r["price"]["on_demand"].update({"USD-Blockchain": 20000.0}), UnknownUnit),
    (lambda r: r["price"]["usd"].update({"on_demand_usd": 9.9}), UnknownUnit),  # two USD prices disagree
    (lambda r: r.update(mrl=100000), UnknownUnit),  # hours expected, not seconds
    (lambda r: r.update(reliability=97), UnknownUnit),  # a percent, not a fraction
    (lambda r: r.update(cuda_version="13"), UnknownUnit),
    (lambda r: r.update(cuda_version=13.2), UnknownFieldType),
    (lambda r: r["price"]["on_demand"].update({"USD-Blockchain": "4.8"}), UnknownFieldType),
    (lambda r: r["price"].update(on_demand=[]), UnknownFieldType),
    (lambda r: r.update(id="1001"), UnknownFieldType),
    (lambda r: r.update(rented="no"), UnknownFieldType),
    (lambda r: r.pop("mrl"), UnknownFieldType),
    (lambda r: r["specs"].update(gpu="RTX 4090"), UnknownFieldType),
    (lambda r: r["specs"].update(gpu="0x NVIDIA GeForce RTX 4090"), UnknownFieldType),
    (lambda r: r["specs"].update(disk="1 TB"), UnknownFieldType),
    (lambda r: r["specs"].update(gpuram="24 GB"), UnknownFieldType),
    (lambda r: r["specs"].update(net=[]), UnknownFieldType),
    (lambda r: r.update(specs=[]), UnknownFieldType),
    (lambda r: r.update(partial_gpu_rental="yes"), UnknownFieldType),
    (lambda r: r.update(partial_gpu_rental={"total_gpus": 2, "available_gpus": 1}), UnknownFieldType),
])
def test_unknown_shapes_and_units_raise_typed_errors(make, error):
    row = server()
    make(row)
    with pytest.raises(error):
        offer_from_row(row)


def test_a_row_that_is_not_an_object_is_refused():
    with pytest.raises(UnknownFieldType):
        offer_from_row("1001")


@pytest.mark.parametrize("payload", [[], {"code": 0}, {"code": 0, "servers": {}}, {"servers": []}])
def test_unknown_marketplace_answers_are_refused(driver, payload):
    with respx.mock as router:
        router.get(MARKET).respond(200, json=payload)
        with pytest.raises(ResponseNotRecognized):
            driver.search(REQ, None, OPTS)


# --- the search --------------------------------------------------------------

def test_search_returns_every_usable_server_and_counts_what_it_leaves_out(driver):
    no_usd = server(2)
    del no_usd["price"]["on_demand"]["USD-Blockchain"]
    with respx.mock as router:
        router.get(MARKET).mock(return_value=answer(
            server(1), no_usd, server(3, gpu="3x Mixed GPUs"), server(4), server(5)))
        found = driver.search(REQ, None, OPTS)
    assert [o.offer_id for o in found.offers] == ["1", "4", "5"]
    assert found.complete is True and found.backend == "clore"
    note = found.note
    assert "1 mixed-GPU rigs" in note and "1 servers with no price in USD" in note
    assert "5% renter fee" in note and "$0.10" in note


def test_search_with_a_cuda_floor_in_the_request_drops_low_versions(driver):
    from dataclasses import replace
    with respx.mock as router:
        router.get(MARKET).mock(return_value=answer(server(1, cuda_version="12.4"), server(2, cuda_version="12.8")))
        found = driver.search(replace(REQ, cuda_version_min=12.8), None, OPTS)
    assert [o.offer_id for o in found.offers] == ["2"]
    assert "below CUDA 12.8" in found.note


def test_the_deny_list_removes_hosts(driver):
    driver.denied_host_ids = {"1"}
    with respx.mock as router:
        router.get(MARKET).mock(return_value=answer(server(1), server(2)))
        found = driver.search(REQ, None, OPTS)
    assert [o.offer_id for o in found.offers] == ["2"]
    assert "local deny list" in found.note


def test_search_without_a_key_sends_no_key_and_a_key_goes_in_the_auth_header(driver):
    with respx.mock as router:
        route = router.get(MARKET).mock(return_value=answer(server(1)))
        driver.search(REQ, None, OPTS)
        driver.search(REQ, KEY, OPTS)
    first, second = (call.request for call in route.calls)
    assert "auth" not in first.headers and "authorization" not in first.headers
    assert second.headers["auth"] == "invented-clore-key" and "authorization" not in second.headers
    assert first.headers["user-agent"].startswith("ablit/")


def test_a_rejected_key_on_search_is_a_credential_error(driver):
    with respx.mock as router:
        router.get(MARKET).respond(401, json={"code": 3})
        with pytest.raises(CredentialsRejected):
            driver.search(REQ, KEY, OPTS)


def test_code_three_in_a_200_answer_is_also_a_credential_error(driver):
    with respx.mock as router:
        router.get(MARKET).respond(200, json={"code": 3})
        with pytest.raises(CredentialsRejected):
            driver.search(REQ, KEY, OPTS)


def test_an_error_code_carries_its_name(driver):
    with respx.mock as router:
        router.get(MARKET).respond(200, json={"code": 6, "error": "server-offline"})
        with pytest.raises(CloreRejected) as error:
            driver.search(REQ, None, OPTS)
    assert error.value.code == 6 and error.value.error == "server-offline" and error.value.definitive


def test_redirects_are_refused(driver):
    with respx.mock as router:
        router.get(MARKET).respond(301, headers={"Location": "https://example.invalid/"})
        with pytest.raises(RequestRejected):
            driver.search(REQ, None, OPTS)


def test_reads_wait_and_retry_after_a_rate_limit_and_the_pacing_uses_the_interval(driver):
    waits = []
    driver._sleep = waits.append
    with respx.mock as router:
        route = router.get(MARKET).mock(side_effect=[
            httpx.Response(200, json={"code": 5}), httpx.Response(500),
            answer(server(1))])
        found = driver.search(REQ, None, OPTS)
    assert route.call_count == 3 and len(found.offers) == 1
    assert waits == [2.0, 4.0]


def test_requests_are_spaced_by_the_interval_the_api_asks_for(driver, monkeypatch):
    import ablit.backends.clore as clore
    assert CloreBackend.__mro__[0] is CloreBackend
    driver.request_interval = 1.1
    monkeypatch.setattr(clore.time, "monotonic", lambda: 10.0)
    pauses = []
    driver._sleep = pauses.append
    with respx.mock as router:
        router.get(MARKET).mock(return_value=answer())
        driver.search(REQ, None, OPTS)
        driver.search(REQ, None, OPTS)
    assert pauses == [1.1]


def test_the_shipped_interval_is_above_one_second():
    from ablit.backends.clore import REQUEST_INTERVAL_SECONDS
    assert REQUEST_INTERVAL_SECONDS > 1.0


# --- requote -----------------------------------------------------------------

FEES = {"code": 0, "renter_fees": {"on_demand": {"base": {"USD-Blockchain": 5, "bitcoin": 5}},
                                   "spot": {"base": {"USD-Blockchain": 1.25}}},
        "creation_fees": {"on_demand": {"USD-Blockchain": 0.1}}}


def test_requote_reads_the_server_again_with_the_account_fees(driver):
    offer = offer_from_row(server(7, usd=4.8))
    higher = copy.deepcopy(FEES)
    higher["renter_fees"]["on_demand"]["base"]["USD-Blockchain"] = 6
    higher["creation_fees"]["on_demand"]["USD-Blockchain"] = 0.25
    with respx.mock as router:
        router.get(MARKET).mock(return_value=answer(server(6), server(7, usd=6.0)))
        fees = router.get(API_ROOT + "/renter_fees").respond(200, json=higher)
        again = driver.requote(offer, KEY)
    assert again.sticker_hourly == pytest.approx(0.25)
    assert again.hourly_all_in == pytest.approx(0.25 * 1.06)
    assert again.fixed_fees == 0.25
    assert fees.calls[0].request.headers["auth"] == "invented-clore-key"


def test_requote_never_quotes_below_the_documented_fee(driver):
    offer = offer_from_row(server(7, usd=4.8))
    lower = copy.deepcopy(FEES)
    lower["renter_fees"]["on_demand"]["base"]["USD-Blockchain"] = 2.5
    with respx.mock as router:
        router.get(MARKET).mock(return_value=answer(server(7, usd=4.8)))
        router.get(API_ROOT + "/renter_fees").respond(200, json=lower)
        assert driver.requote(offer, KEY).hourly_all_in == pytest.approx(0.2 * 1.05)


def test_requote_returns_none_when_the_server_is_gone_or_rented(driver):
    offer = offer_from_row(server(7))
    with respx.mock as router:
        router.get(MARKET).mock(side_effect=[answer(server(8)), answer(server(7, rented=True))])
        router.get(API_ROOT + "/renter_fees").respond(200, json=FEES)
        assert driver.requote(offer, KEY) is None
        assert driver.requote(offer, KEY) is None


@pytest.mark.parametrize("fees", [{"code": 0}, {"code": 0, "renter_fees": {"on_demand": {"base": {}}},
                                                  "creation_fees": {"on_demand": {"USD-Blockchain": 0.1}}}])
def test_requote_refuses_an_unknown_fee_shape(driver, fees):
    offer = offer_from_row(server(7))
    with respx.mock as router:
        router.get(MARKET).mock(return_value=answer(server(7)))
        router.get(API_ROOT + "/renter_fees").respond(200, json=fees)
        with pytest.raises(UnknownFieldType):
            driver.requote(offer, KEY)


# --- the plan command ---------------------------------------------------------

def test_plan_with_clore_only_prices_the_offer_with_fee_and_creation_fee(capsys):
    row = server(31, usd=24.0)
    row["specs"].update(ram=256, disk="NVME 2000GB", gpuram=24)
    with respx.mock as router:
        router.get(MARKET).mock(return_value=answer(row))
        code = main(["plan", "llama31-8b-abliterated-q4km", "--backends", "clore", "--json"],
                    backends={"clore": CloreBackend(httpx.Client())}, environ={})
    out = json.loads(capsys.readouterr().out)
    assert code == 0
    chosen = out["chosen"]
    assert chosen["backend"] == "clore" and chosen["offer_id"] == "31"
    assert chosen["gpu_only_hourly_usd"] == pytest.approx(1.0)  # 24 dollars a day
    assert chosen["hourly_all_in_usd"] == pytest.approx(1.05)  # with the 5% renter fee
    assert chosen["one_time_fees_usd"] == pytest.approx(0.10)
    assert chosen["verified"] is False
    assert "referral" not in json.dumps(out).lower()


def test_a_default_plan_searches_clore_with_no_key_set():
    assert "clore" in default_backends()


# --- setup -------------------------------------------------------------------

WALLETS = API_ROOT + "/wallets"


def _tty(monkeypatch, answers, key="invented-setup-key"):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)
    queue = iter(answers)
    monkeypatch.setattr("builtins.input", lambda prompt: next(queue))
    monkeypatch.setattr(keys.getpass, "getpass", lambda prompt: key)


def test_setup_clore_tests_the_key_with_one_read_only_call_and_saves_it(monkeypatch, capsys):
    _tty(monkeypatch, ["yes", "yes"])
    with respx.mock as router:
        route = router.get(WALLETS).respond(200, json={"code": 0, "wallets": [{"name": "USD", "balance": 0}]})
        assert main(["setup", "clore"], environ={}) == 0
        assert route.call_count == 1
        sent = route.calls[0].request
        assert sent.method == "GET" and sent.headers["auth"] == "invented-setup-key"
        assert "authorization" not in sent.headers
    assert keys.secret_get("accounts/clore") == "invented-setup-key"
    assert keys.credentials("clore", {}).api_key == "invented-setup-key"
    out = capsys.readouterr().out
    assert "invented-setup-key" not in out and "Saved the Clore key" in out


def test_setup_clore_can_keep_the_key_in_this_process_only(monkeypatch, capsys):
    _tty(monkeypatch, ["yes", "no"])
    with respx.mock as router:
        router.get(WALLETS).respond(200, json={"code": 0, "wallets": []})
        assert main(["setup", "clore"], environ={}) == 0
    assert keys.secret_get("accounts/clore") is None
    assert keys.SESSION["clore"] == "invented-setup-key"
    assert "CLORE_API_KEY" in capsys.readouterr().out


@pytest.mark.parametrize("response", [httpx.Response(401, json={"code": 3}), httpx.Response(200, json={"code": 3})])
def test_setup_clore_exits_4_when_the_key_is_rejected(monkeypatch, capsys, response):
    _tty(monkeypatch, ["yes", "yes"])
    with respx.mock as router:
        router.get(WALLETS).mock(return_value=response)
        assert main(["setup", "clore"], environ={}) == 4
    assert keys.secret_get("accounts/clore") is None
    assert "invented-setup-key" not in capsys.readouterr().err


def test_setup_clore_reports_an_unrecognized_answer_as_a_failed_test(monkeypatch):
    _tty(monkeypatch, ["yes", "yes"])
    with respx.mock as router:
        router.get(WALLETS).respond(200, json={"code": 0})
        assert main(["setup", "clore"], environ={}) == 4
    assert keys.secret_get("accounts/clore") is None


def test_setup_clore_with_no_key_typed_exits_4(monkeypatch):
    _tty(monkeypatch, ["yes"], key="")
    assert main(["setup", "clore"], environ={}) == 4


def test_the_environment_key_wins_over_the_keyring_and_a_missing_key_names_the_variable():
    keys.secret_set("accounts/clore", "invented-keyring-key")
    assert keys.credentials("clore", {}).api_key == "invented-keyring-key"
    assert keys.credentials("clore", {"CLORE_API_KEY": "invented-env-key"}).api_key == "invented-env-key"
    keys.secret_delete("accounts/clore")
    with pytest.raises(keys.CredentialError, match="CLORE_API_KEY"):
        keys.credentials("clore", {})
    with pytest.raises(keys.CredentialError, match="VAST_API_KEY"):
        keys.credentials("vast", {})


def test_a_plan_reads_the_clore_key_from_the_environment(driver):
    with respx.mock as router:
        route = router.get(MARKET).mock(return_value=answer())
        from ablit.backends import creds_from_env
        driver.search(REQ, creds_from_env("clore", {"CLORE_API_KEY": "invented-env-key"}), OPTS)
    assert route.calls[0].request.headers["auth"] == "invented-env-key"


def test_no_account_flow_shows_the_direct_link_and_no_paid_link(monkeypatch, capsys):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)
    answers = iter(["no", "2"])
    monkeypatch.setattr("builtins.input", lambda prompt: next(answers))
    with respx.mock:
        assert main(["setup", "clore", "--no-referral"], environ={}) == 0
    out = capsys.readouterr().out
    assert "https://clore.ai/" in out and "paid link" not in out.lower()
    assert json.loads((RunStore().root / "config.json").read_text())["referral"] is False


def test_no_account_flow_with_an_id_shows_the_disclosure_before_the_link(monkeypatch, capsys):
    from ablit import referral
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)
    answers = iter(["no", "3"])
    monkeypatch.setattr("builtins.input", lambda prompt: next(answers))
    monkeypatch.setattr(referral, "load_ids", lambda text=None: {"clore": "cloreid01", "runpod": "", "vast": ""})
    assert keys.setup_clore(CloreBackend(httpx.Client()), RunStore(), False, {}) == 0
    out = capsys.readouterr().out
    assert out.index("Paid link:") < out.index("ref_id=cloreid01")
    assert "[1] Sign up with the paid link" in out and "[3] Skip" in out


def test_trust_verified_drops_every_clore_offer(driver):
    from ablit.select import apply_filter_chain
    from dataclasses import replace
    with respx.mock as router:
        router.get(MARKET).mock(return_value=answer(server(1)))
        found = driver.search(REQ, None, OPTS)
    kept = apply_filter_chain(found.offers, REQ, replace(OPTS, trust="verified")).kept
    assert kept == []
