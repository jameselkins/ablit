"""The RunPod driver, tested against invented responses."""

from __future__ import annotations

import dataclasses

import httpx
import pytest
import respx

from ablit.backends import Creds
from ablit.backends.errors import (
    CredentialsRejected,
    RequestRejected,
    ResponseNotRecognized,
    UnknownFieldType,
)
from ablit.backends.runpod import (
    HOURS_PER_MONTH,
    VOLUME_DISK_USD_PER_GB_MONTH,
    RunPodBackend,
    build_query,
    offer_id_for,
    parse_offer_id,
    volume_disk_hourly,
)
from ablit.select import HardwareRequest, PlanOptions, apply_filter_chain
from market_fixtures import RUNPOD_URL, FakeRunPod, runpod_response, runpod_type

REQ = HardwareRequest(
    gpu_vendor="nvidia", gpu_count=2, vram_gb_each=48, vram_gb_total=96, host_ram_gb=64, disk_gb=72.7,
    download_gb=72.7, startup_hours=0.5, min_download_mbps=500,
)
OPTS = PlanOptions()
DISK = 73  # 72.7 rounded up


def backend_for(fake) -> tuple[RunPodBackend, respx.MockRouter]:
    router = respx.mock(assert_all_called=False)
    router.post(RUNPOD_URL).mock(side_effect=fake)
    return RunPodBackend(httpx.Client()), router


def test_volume_disk_rate_is_ten_cents_per_gb_per_month_over_730_hours():
    assert VOLUME_DISK_USD_PER_GB_MONTH == 0.10 and HOURS_PER_MONTH == 730
    assert volume_disk_hourly(730) == pytest.approx(0.10)
    assert volume_disk_hourly(73) == pytest.approx(0.01)


def test_the_price_is_the_total_for_all_gpus_plus_disk_and_is_not_multiplied_again():
    fake = FakeRunPod([runpod_type("NVIDIA RTX 6000 Ada Generation", "RTX 6000 Ada", 48, price=1.60, pod_ram=120)])
    backend, router = backend_for(fake)
    with router:
        found = backend.search(REQ, None, OPTS)
    assert found.complete
    secure = [o for o in found.offers]
    assert len(secure) == 1  # both tiers gave the same price, so one offer remains
    offer = secure[0]
    assert offer.gpu_count == 2
    assert offer.sticker_hourly == pytest.approx(1.60)
    assert offer.hourly_all_in == pytest.approx(1.60 + 73 * 0.10 / 730)
    assert offer.bandwidth_cost_per_gb == 0.0
    assert offer.download_mbps is None  # RunPod advertises no speed
    assert offer.host_ram_gb == 120 and offer.gpu_vram_gb_each == 48
    assert offer.disk_gb == offer.allocated_disk_gb == DISK
    assert offer.compute_capability == 8.9  # from the table in runpod_gpus.py
    assert offer.remaining_hours is None


def test_two_queries_one_with_secure_cloud_and_one_without():
    fake = FakeRunPod([runpod_type(price=1.0)], [runpod_type(price=1.5)])
    backend, router = backend_for(fake)
    with router:
        found = backend.search(REQ, None, OPTS)
    assert len(fake.queries) == 2
    plain, secure = fake.queries
    assert "secureCloud" not in plain and "secureCloud: true" in secure
    for query in fake.queries:
        assert "gpuCount: 2" in query and "minMemoryInGb: 64" in query
        assert "mutation" not in query.lower()
        assert "uninterruptablePrice" in query and "stockStatus" in query
    by_tier = {parse_offer_id(o.offer_id)[2]: o for o in found.offers}
    assert set(by_tier) == {"any", "secure"}
    assert by_tier["any"].sticker_hourly == 1.0 and by_tier["any"].verified is False
    assert by_tier["secure"].sticker_hourly == 1.5 and by_tier["secure"].verified is True


def test_equal_prices_in_both_tiers_give_one_secure_offer():
    fake = FakeRunPod([runpod_type(price=1.0)])
    backend, router = backend_for(fake)
    with router:
        found = backend.search(REQ, None, OPTS)
    assert [parse_offer_id(o.offer_id)[2] for o in found.offers] == ["secure"]


def test_types_without_a_price_are_not_offers():
    fake = FakeRunPod([runpod_type(price=None), runpod_type("NVIDIA L40S", "L40S", 48, price=0.9)])
    backend, router = backend_for(fake)
    with router:
        found = backend.search(REQ, None, OPTS)
    assert {o.gpu_model for o in found.offers} == {"L40S"}
    assert found.note and "1 in any cloud" in found.note and "1 in Secure Cloud" in found.note


def test_a_priced_type_with_no_stock_is_not_rentable():
    fake = FakeRunPod([runpod_type("NVIDIA L40S", "L40S", 48, price=0.9, stock=None, pod_ram=100)])
    backend, router = backend_for(fake)
    with router:
        found = backend.search(REQ, None, OPTS)
    assert found.offers and not found.offers[0].rentable
    result = apply_filter_chain(found.offers, REQ, OPTS)
    assert "not rentable" in result.steps[3].reasons


def test_vram_is_per_gpu_and_the_filter_chain_applies_it():
    fake = FakeRunPod([runpod_type("NVIDIA GeForce RTX 4090", "RTX 4090", 24, price=0.8, pod_ram=100)])
    backend, router = backend_for(fake)
    with router:
        found = backend.search(REQ, None, OPTS)
    assert not apply_filter_chain(found.offers, REQ, OPTS).kept


def test_amd_and_nvidia_vendors_come_from_the_type_id():
    fake = FakeRunPod([
        runpod_type("AMD Instinct MI350 OAM", "MI350X", 294, price=2.0),
        runpod_type("Tesla V100-SXM2-16GB", "V100 SXM2", 16, price=0.3),
    ])
    backend, router = backend_for(fake)
    with router:
        found = backend.search(REQ, None, OPTS)
    vendors = {o.gpu_model: o.gpu_vendor for o in found.offers}
    assert vendors == {"MI350X": "amd", "V100 SXM2": "nvidia"}


def test_key_is_sent_only_when_one_is_configured():
    fake = FakeRunPod([runpod_type()])
    backend, router = backend_for(fake)
    with router:
        backend.search(REQ, None, OPTS)
        backend.search(REQ, Creds(api_key="invented-test-key"), OPTS)
    assert "authorization" not in fake.headers[0]
    assert fake.headers[-1]["authorization"] == "Bearer invented-test-key"


def test_requote_runs_the_query_again_for_that_type_and_count():
    fake = FakeRunPod([runpod_type("NVIDIA L40S", "L40S", 48, price=0.95, pod_ram=70)])
    backend, router = backend_for(fake)
    with router:
        found = backend.search(REQ, None, OPTS)
        offer = found.offers[0]
        fake.queries.clear()
        fake.secure = fake.any_cloud = [runpod_type("NVIDIA L40S", "L40S", 48, price=1.05, pod_ram=70)]
        again = backend.requote(offer, Creds(api_key="invented-test-key"))
    assert len(fake.queries) == 1
    assert 'id: "NVIDIA L40S"' in fake.queries[0] and "gpuCount: 2" in fake.queries[0]
    assert "secureCloud: true" in fake.queries[0]  # the offer was a Secure Cloud one
    assert again is not None and again.sticker_hourly == pytest.approx(1.05)
    assert again.offer_id == offer.offer_id


def test_requote_returns_none_when_the_price_is_gone():
    fake = FakeRunPod([runpod_type("NVIDIA L40S", "L40S", 48, price=0.95)])
    backend, router = backend_for(fake)
    with router:
        offer = backend.search(REQ, None, OPTS).offers[0]
        fake.secure = fake.any_cloud = [runpod_type("NVIDIA L40S", "L40S", 48, price=None)]
        assert backend.requote(offer, Creds(api_key="invented-test-key")) is None


def test_offer_ids_round_trip():
    assert parse_offer_id(offer_id_for("NVIDIA A40", 4, "any")) == ("NVIDIA A40", 4, "any")
    with pytest.raises(ValueError):
        parse_offer_id("12345")


def test_query_text_is_a_query_and_escapes_the_type_id():
    text = build_query(1, 16, secure=False, gpu_type_id='odd "name"')
    assert text.startswith("query") and "mutation" not in text
    assert r'id: "odd \"name\""' in text


@pytest.mark.parametrize(
    "bad_type",
    [
        {**runpod_type(), "memoryInGb": "24GB"},
        {**runpod_type(), "id": 7},
        {**runpod_type(), "lowestPrice": "cheap"},
        {**runpod_type(price="0.5")},
        {**runpod_type("Mystery Accelerator", price=1.0)},
        {**runpod_type(pod_ram=None)},
    ],
)
def test_unknown_types_are_refused(bad_type):
    fake = FakeRunPod([bad_type])
    backend, router = backend_for(fake)
    with router, pytest.raises(UnknownFieldType):
        backend.search(REQ, None, OPTS)


def test_graphql_errors_and_bad_shapes_are_typed_errors():
    router = respx.mock()
    router.post(RUNPOD_URL).mock(return_value=httpx.Response(200, json={"errors": [{"message": "Field is not defined"}]}))
    with router, pytest.raises(RequestRejected, match="Field is not defined"):
        RunPodBackend(httpx.Client()).search(REQ, None, OPTS)
    router = respx.mock()
    router.post(RUNPOD_URL).mock(return_value=httpx.Response(200, json={"data": {"other": []}}))
    with router, pytest.raises(ResponseNotRecognized):
        RunPodBackend(httpx.Client()).search(REQ, None, OPTS)
    router = respx.mock()
    router.post(RUNPOD_URL).mock(return_value=httpx.Response(401, json={"error": "no"}))
    with router, pytest.raises(CredentialsRejected):
        RunPodBackend(httpx.Client()).search(REQ, Creds(api_key="invented-test-key"), OPTS)


def test_a_runpod_search_is_complete():
    assert runpod_response([]).status_code == 200
    fake = FakeRunPod([])
    backend, router = backend_for(fake)
    with router:
        found = backend.search(REQ, None, OPTS)
    assert found.complete and found.offers == []


# --- compute capability table ---------------------------------------------


def test_a_known_type_gets_its_compute_capability():
    from ablit.backends.runpod_gpus import compute_capability_for

    assert compute_capability_for("NVIDIA GeForce RTX 4090") == 8.9
    assert compute_capability_for("NVIDIA H100 PCIe") == 9.0
    assert compute_capability_for("NVIDIA RTX A6000") == 8.6
    assert compute_capability_for("Tesla V100-SXM2-16GB") == 7.0
    fake = FakeRunPod([runpod_type("NVIDIA L40S", "L40S", 48, price=0.9)])
    backend, router = backend_for(fake)
    with router:
        offers = backend.search(REQ, None, OPTS).offers
    assert {o.compute_capability for o in offers} == {8.9}


def test_an_unknown_type_keeps_no_compute_capability_and_the_filter_says_why():
    from ablit.backends.runpod_gpus import compute_capability_for

    assert compute_capability_for("NVIDIA Invented Accelerator") is None
    fake = FakeRunPod([runpod_type("NVIDIA Invented Accelerator", "Invented", 48, price=0.9)])
    backend, router = backend_for(fake)
    with router:
        offers = backend.search(REQ, None, OPTS).offers
    assert offers and offers[0].compute_capability is None
    needs_8_9 = HardwareRequest(**{**REQ.__dict__, "compute_capability_min": 8.9})
    result = apply_filter_chain(offers, needs_8_9, OPTS)
    assert not result.kept
    assert result.steps[0].reasons == {"compute capability unknown": len(offers)}


def test_every_table_entry_has_a_sane_capability():
    from ablit.backends.runpod_gpus import COMPUTE_CAPABILITY_BY_RUNPOD_ID, NVIDIA_LIST_URLS

    assert all(7.0 <= value <= 12.1 for value in COMPUTE_CAPABILITY_BY_RUNPOD_ID.values())
    assert NVIDIA_LIST_URLS[0] == "https://developer.nvidia.com/cuda-gpus"


def test_only_secure_cloud_offers_are_reachable_over_ssh():
    fake = FakeRunPod([runpod_type(price=1.0)], [runpod_type(price=1.5)])
    backend, router = backend_for(fake)
    with router:
        found = backend.search(REQ, None, OPTS)
    reachable = {parse_offer_id(o.offer_id)[2]: o.ssh_reachable for o in found.offers}
    assert reachable == {"any": False, "secure": True}


def test_a_community_offer_stays_in_the_plan_with_a_drop_reason():
    from ablit.select import apply_filter_chain

    fake = FakeRunPod([runpod_type(price=1.0)], [runpod_type(price=1.5)])
    backend, router = backend_for(fake)
    with router:
        found = backend.search(REQ, None, OPTS)
    # Give the offers enough hardware that only the SSH rule can remove one.
    roomy = [
        dataclasses.replace(o, gpu_vram_gb_each=200, compute_capability=9.0, host_ram_gb=1000, disk_gb=1000)
        for o in found.offers
    ]
    result = apply_filter_chain(roomy, REQ, OPTS)
    assert [parse_offer_id(o.offer_id)[2] for o in result.kept] == ["secure"]
    assert "any" not in [parse_offer_id(o.offer_id)[2] for o in result.kept]
    reasons = {r for step in result.steps for r in step.reasons}
    assert "Community Cloud pods may have no public IP for SSH" in reasons
