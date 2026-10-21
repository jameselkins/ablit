"""The Vast driver, tested against invented responses."""

from __future__ import annotations

import random

import httpx
import pytest
import respx

from ablit.backends import Creds
from ablit.backends.errors import (
    CredentialsRejected,
    RequestRejected,
    ResponseNotRecognized,
    UnknownFieldType,
    UnknownUnit,
)
from ablit.backends.vast import PagingStopped, VastBackend, build_body, offer_from_row
from ablit.select import HardwareRequest, PlanOptions, apply_filter_chain, rank_offers
from market_fixtures import VAST_URL, FakeVast, vast_row

REQ = HardwareRequest(
    gpu_vendor="nvidia",
    gpu_count=1,
    vram_gb_each=24,
    host_ram_gb=90,
    disk_gb=574.5,
    download_gb=370.0,
    startup_hours=0.5,
    min_download_mbps=500,
    compute_capability_min=7.5,
)
OPTS = PlanOptions(hours=1.0)
ALLOCATED = 575


def backend_for(fake) -> tuple[VastBackend, respx.MockRouter]:
    router = respx.mock(assert_all_called=False)
    router.post(VAST_URL).mock(side_effect=fake)
    return VastBackend(httpx.Client()), router


# --- unit conversion -----------------------------------------------------


def test_row_is_converted_from_vast_units():
    row = vast_row(
        9001, base=0.50, allocated=ALLOCATED,
        gpu_ram=24564, gpu_total_ram=49128, num_gpus=2, cpu_ram=128000, disk_space=900.5,
        inet_down=1200.0, inet_down_cost=0.01, internet_down_cost_per_tb=10.24,
        compute_cap=890, duration=7200.0, reliability=0.97, verification="verified", geolocation="Texas, US",
    )
    offer = offer_from_row(row, ALLOCATED)
    assert offer.backend == "vast" and offer.offer_id == "9001"
    assert offer.gpu_model == "RTX 4090" and offer.gpu_count == 2
    assert offer.gpu_vram_gb_each == pytest.approx(24.564)  # MB to decimal GB, per GPU
    assert offer.host_ram_gb == pytest.approx(128.0)
    assert offer.disk_gb == pytest.approx(900.5)  # already in GB
    assert offer.compute_capability == pytest.approx(8.9)  # sent and read multiplied by 100
    assert offer.remaining_hours == pytest.approx(2.0)  # seconds to hours
    assert offer.download_mbps == pytest.approx(1200.0)
    assert offer.bandwidth_cost_per_gb == pytest.approx(0.01)
    assert offer.hourly_all_in == pytest.approx(row["dph_total"])
    assert offer.sticker_hourly == pytest.approx(0.50)
    assert offer.verified is True and offer.reliability == pytest.approx(0.97)
    assert offer.region == "US"
    assert offer.allocated_disk_gb == ALLOCATED
    assert offer.rentable is True and offer.interruptible is False and offer.gpu_fraction == 1.0


@pytest.mark.parametrize("label,expected", [("verified", True), ("unverified", False), ("deverified", False)])
def test_verification_labels(label, expected):
    assert offer_from_row(vast_row(verification=label), ALLOCATED).verified is expected


def test_a_24_gb_card_passes_a_24_gb_floor():
    # 24564 MB is what a 24 GB card reports. Dividing by 1024 would give 23.99 and drop every such card.
    offer = offer_from_row(vast_row(gpu_ram=24564, gpu_total_ram=24564), ALLOCATED)
    assert apply_filter_chain([offer], REQ, OPTS).kept == [offer]


def test_missing_compute_capability_is_kept_as_unknown():
    offer = offer_from_row(vast_row(compute_cap=None), ALLOCATED)
    assert offer.compute_capability is None


def test_shared_gpu_rows_are_read_and_filtered_out():
    offer = offer_from_row(vast_row(gpu_frac=0.5), ALLOCATED)
    assert offer.gpu_fraction == 0.5
    result = apply_filter_chain([offer], REQ, OPTS)
    assert not result.kept and "GPU is shared with other renters" in result.steps[0].reasons


# --- the request body ----------------------------------------------------


def test_request_body_for_one_profile():
    body = build_body(REQ, OPTS)
    assert body == {
        "rentable": {"eq": True},
        "type": "on-demand",
        "gpu_frac": {"eq": 1},
        "gpu_arch": {"eq": "nvidia"},
        "cpu_arch": {"eq": "amd64"},
        "num_gpus": {"eq": 1},
        "gpu_ram": {"gte": 24000},
        "cpu_ram": {"gte": 90000},
        "disk_space": {"gte": 575},
        "allocated_storage": 575,
        "inet_down": {"gte": 500},
        "duration": {"gte": 5400},
        "compute_cap": {"gte": 750},
        "order": [["dph_base", "asc"]],
    }


def test_request_body_carries_every_user_filter_and_the_total_vram():
    req = HardwareRequest(
        gpu_vendor="nvidia", gpu_count=8, vram_gb_each=80, vram_gb_total=614, host_ram_gb=256, disk_gb=510.3,
        download_gb=510.0, startup_hours=1.5, min_download_mbps=500,
    )
    opts = PlanOptions(hours=2.0, trust="verified", min_reliability=0.95, region="us", max_price_hr=12.5,
                       min_download_mbps=800)
    body = build_body(req, opts, base_cursor=3.25)
    assert body["gpu_total_ram"] == {"gte": 614000}
    assert body["verified"] == {"eq": True}
    assert body["reliability"] == {"gte": 0.95}
    assert body["geolocation"] == {"in": ["US"]}
    assert body["inet_down"] == {"gte": 800}
    assert body["duration"] == {"gte": int(3.5 * 3600)}
    assert body["dph_base"] == {"gte": 3.25, "lte": 12.5}
    assert body["allocated_storage"] == body["disk_space"]["gte"] == 511
    assert "compute_cap" not in body


def test_the_search_bounds_the_cuda_version_and_the_gpu_generation_from_below():
    req = HardwareRequest(
        gpu_vendor="nvidia", gpu_count=1, vram_gb_each=24, host_ram_gb=90, disk_gb=393.7, download_gb=370.0,
        startup_hours=0.3, min_download_mbps=1000, cuda_version_min=13.0, cuda_architectures=(120, 86, 89),
    )
    body = build_body(req, OPTS)
    # The Vast field cuda_max_good is the highest CUDA version the host driver supports.
    assert body["cuda_max_good"] == {"gte": 13.0}
    # The smallest generation in the image sets the floor, as compute capability times 100.
    assert body["compute_cap"] == {"gte": 860}
    assert body["inet_down"] == {"gte": 1000}
    # A larger floor from the entry wins over the image floor.
    stricter = HardwareRequest(**{**req.__dict__, "compute_capability_min": 8.9})
    assert build_body(stricter, OPTS)["compute_cap"] == {"gte": 890}
    assert "cuda_max_good" not in build_body(HardwareRequest(**{**req.__dict__, "cuda_version_min": None}), OPTS)


def test_key_is_sent_only_when_one_is_configured():
    fake = FakeVast([vast_row(9001)])
    backend, router = backend_for(fake)
    with router:
        backend.search(REQ, None, OPTS)
        backend.search(REQ, Creds(api_key="invented-test-key"), OPTS)
    assert "authorization" not in fake.headers[0]
    assert fake.headers[1]["authorization"] == "Bearer invented-test-key"


def test_key_never_appears_in_a_repr():
    assert "invented-test-key" not in repr(Creds(api_key="invented-test-key"))


# --- paging --------------------------------------------------------------


def _market(seed: int, count: int, high_storage_share: bool = True) -> list[dict]:
    """Rows whose all-in order differs from their GPU-only order, because storage prices differ."""
    rnd = random.Random(seed)
    rows = []
    for i in range(count):
        base = round(0.30 + rnd.random() * 2.0, 4)
        storage = rnd.choice([0.02, 0.1, 0.4, 1.2]) if high_storage_share else 0.05
        rows.append(
            vast_row(
                100000 + i, base=base, allocated=ALLOCATED, storage_cost=storage,
                inet_down_cost=rnd.choice([0.0, 0.0005, 0.004]), internet_down_cost_per_tb=None,
            )
        )
    for row in rows:
        row.pop("internet_down_cost_per_tb")
    return rows


def _brute_force_best(rows: list[dict], opts: PlanOptions = OPTS):
    offers = [offer_from_row(r, ALLOCATED) for r in rows]
    return rank_offers(offers, REQ, opts).ranked[0].offer


@pytest.mark.parametrize("seed", range(8))
def test_paged_search_finds_the_cheapest_total_in_the_whole_market(seed):
    rows = _market(seed, 400)
    fake = FakeVast(rows)
    backend, router = backend_for(fake)
    with router:
        found = backend.search(REQ, None, OPTS)
    assert found.complete
    ranked = rank_offers(found.offers, REQ, OPTS).ranked
    assert ranked[0].offer == _brute_force_best(rows)
    # Later pages carry the GPU-only price of the last row read, inclusive.
    cursors = [b["dph_base"]["gte"] for b in fake.bodies[1:] if "dph_base" in b]
    assert cursors == sorted(cursors)
    assert all(b["order"] == [["dph_base", "asc"]] for b in fake.bodies)


def test_rows_with_a_low_gpu_price_and_a_high_all_in_price_are_not_skipped():
    # A cursor on the all-in price would drop these rows, because the server's price filter
    # compares the GPU-only price. Here the first 64 rows are cheap in both prices, and the
    # needle has a GPU-only price below the last all-in price of page one.
    cheap = [vast_row(1000 + i, base=1.0 + i * 0.01, storage_cost=0.01, inet_down_cost=0.5,
                      internet_down_cost_per_tb=None) for i in range(64)]
    needle = vast_row(2000, base=1.20, storage_cost=1.0, inet_down_cost=0.0, internet_down_cost_per_tb=None)
    for row in cheap + [needle]:
        row.pop("internet_down_cost_per_tb")
    rows = cheap + [needle]
    last_all_in = max(r["dph_total"] for r in cheap)
    assert needle["dph_base"] < last_all_in < needle["dph_total"]
    fake = FakeVast(rows)
    backend, router = backend_for(fake)
    with router:
        found = backend.search(REQ, None, OPTS)
    assert "2000" in {o.offer_id for o in found.offers}
    assert rank_offers(found.offers, REQ, OPTS).ranked[0].offer.offer_id == "2000"
    assert found.complete


def test_a_short_first_page_is_complete_after_one_query():
    fake = FakeVast([vast_row(1 + i, base=0.5 + i * 0.1) for i in range(10)])
    backend, router = backend_for(fake)
    with router:
        found = backend.search(REQ, None, OPTS)
    assert found.complete and len(found.offers) == 10 and len(fake.bodies) == 1 and found.note is None


def _expensive_transfer_market(count: int = 200) -> list[dict]:
    # A large per-GB price keeps the lowest total above the stopping bound, so the search keeps paging.
    rows = [vast_row(5000 + i, base=1.0 + i * 0.01, inet_down_cost=0.5, internet_down_cost_per_tb=None)
            for i in range(count)]
    for row in rows:
        row.pop("internet_down_cost_per_tb")
    return rows


def test_server_refusing_the_later_page_filter_labels_the_result_incomplete():
    fake = FakeVast(_expensive_transfer_market(), reject_cursor=True)
    backend, router = backend_for(fake)
    with router:
        found = backend.search(REQ, None, OPTS)
    assert not found.complete
    assert found.offers  # the rows read before the refusal are still returned
    assert found.note == f"cheapest of the {len(found.offers)} offers checked"


def test_server_ignoring_the_later_page_filter_labels_the_result_incomplete():
    fake = FakeVast(_expensive_transfer_market(), ignore_cursor=True)
    backend, router = backend_for(fake)
    with router:
        found = backend.search(REQ, None, OPTS)
    assert not found.complete
    assert found.note and found.note.startswith("cheapest of the ")
    assert len(fake.bodies) >= 2  # it did try the later page


def test_later_page_returning_lower_prices_raises_filter_rejected_in_the_fetcher():
    fake = FakeVast(_expensive_transfer_market(), ignore_cursor=True)
    backend, router = backend_for(fake)
    fetch = backend.page_fetcher(REQ, None, OPTS)
    after = None
    with router, pytest.raises(PagingStopped) as stopped:
        for _ in range(5):
            page = fetch(after)
            after = page[-1].hourly_all_in
    assert "below the price" in str(stopped.value)


def test_a_first_page_error_is_a_backend_error_not_a_filter_rejection():
    router = respx.mock()
    router.post(VAST_URL).mock(return_value=httpx.Response(400, json={"error": "bad_request", "msg": "nope"}))
    with router, pytest.raises(RequestRejected):
        VastBackend(httpx.Client()).search(REQ, None, OPTS)


def test_more_than_64_rows_at_one_gpu_price_stops_the_scan_without_claiming_completeness():
    rows = [vast_row(7000 + i, base=1.0, inet_down_cost=0.5, internet_down_cost_per_tb=None) for i in range(80)]
    for row in rows:
        row.pop("internet_down_cost_per_tb")
    fake = FakeVast(rows)
    backend, router = backend_for(fake)
    with router:
        found = backend.search(REQ, None, OPTS)
    assert not found.complete


def test_credentials_rejected_is_its_own_error():
    router = respx.mock()
    router.post(VAST_URL).mock(return_value=httpx.Response(401, json={"error": "unauthorized"}))
    with router, pytest.raises(CredentialsRejected):
        VastBackend(httpx.Client()).search(REQ, Creds(api_key="invented-test-key"), OPTS)


# --- unknown types and units ---------------------------------------------


@pytest.mark.parametrize(
    "overrides,error",
    [
        ({"num_gpus": "one"}, UnknownFieldType),
        ({"num_gpus": True}, UnknownFieldType),
        ({"gpu_ram": "24GB"}, UnknownFieldType),
        ({"dph_total": None}, UnknownFieldType),
        ({"dph_total": float("nan")}, UnknownFieldType),
        ({"rentable": "yes"}, UnknownFieldType),
        ({"verification": "gold"}, UnknownFieldType),
        ({"gpu_arch": "tpu"}, UnknownFieldType),
        ({"id": "abc"}, UnknownFieldType),
        ({"gpu_total_ram": 99999}, UnknownUnit),  # does not match gpu_ram times num_gpus
        ({"inet_down_cost": 4.0}, UnknownUnit),  # looks like a per-TB price, and disagrees with the per-TB field
        ({"reliability": 99.5}, UnknownUnit),  # a percentage, not a fraction
        ({"compute_cap": 8.9}, UnknownUnit),  # not multiplied by 100
        ({"gpu_frac": 3}, UnknownUnit),
        ({"dph_total": 0.1, "dph_base": 0.5}, UnknownUnit),  # all-in below GPU-only
    ],
)
def test_unknown_types_and_units_are_refused(overrides, error):
    with pytest.raises(error):
        offer_from_row(vast_row(**overrides), ALLOCATED)


def test_a_missing_field_is_refused():
    row = vast_row()
    del row["dph_base"]
    with pytest.raises(UnknownFieldType, match="dph_base"):
        offer_from_row(row, ALLOCATED)


def test_a_row_that_is_not_an_object_is_refused():
    with pytest.raises(UnknownFieldType):
        offer_from_row(["not", "a", "row"], ALLOCATED)


def test_search_fails_with_a_typed_error_instead_of_guessing():
    fake = FakeVast([vast_row(1), vast_row(2, num_gpus="two")])
    backend, router = backend_for(fake)
    with router, pytest.raises(UnknownFieldType):
        backend.search(REQ, None, OPTS)


def test_unordered_rows_are_refused():
    rows = [vast_row(1, base=2.0), vast_row(2, base=1.0)]
    router = respx.mock()
    router.post(VAST_URL).mock(return_value=httpx.Response(200, json={"offers": rows}))
    with router, pytest.raises(ResponseNotRecognized, match="not ordered"):
        VastBackend(httpx.Client()).search(REQ, None, OPTS)


def test_a_response_without_an_offers_list_is_refused():
    router = respx.mock()
    router.post(VAST_URL).mock(return_value=httpx.Response(200, json={"results": []}))
    with router, pytest.raises(ResponseNotRecognized):
        VastBackend(httpx.Client()).search(REQ, None, OPTS)


# --- requote -------------------------------------------------------------


def test_requote_searches_for_the_offer_with_the_same_allocated_disk():
    offer = offer_from_row(vast_row(9001, base=0.5), ALLOCATED)
    fake = FakeVast([vast_row(9001, base=0.55)])
    backend, router = backend_for(fake)
    with router:
        again = backend.requote(offer, Creds(api_key="invented-test-key"))
    body = fake.bodies[0]
    assert body["ask_contract_id"] == {"eq": 9001}
    assert body["allocated_storage"] == ALLOCATED
    assert body["rentable"] == {"eq": True}
    assert again is not None and again.offer_id == "9001"
    assert again.sticker_hourly == pytest.approx(0.55)


def test_requote_returns_none_when_the_offer_is_gone():
    offer = offer_from_row(vast_row(9001), ALLOCATED)
    fake = FakeVast([])
    backend, router = backend_for(fake)
    with router:
        assert backend.requote(offer, Creds(api_key="invented-test-key")) is None


def test_requote_ignores_a_different_offer_in_the_answer():
    offer = offer_from_row(vast_row(9001), ALLOCATED)
    fake = FakeVast([vast_row(9002)])
    backend, router = backend_for(fake)
    with router:
        assert backend.requote(offer, Creds(api_key="invented-test-key")) is None


def test_requote_needs_the_allocated_disk():
    offer = offer_from_row(vast_row(9001), ALLOCATED)
    bare = type(offer)(**{**offer.__dict__, "allocated_disk_gb": None})
    with pytest.raises(ValueError):
        VastBackend(httpx.Client()).requote(bare, Creds(api_key="invented-test-key"))
