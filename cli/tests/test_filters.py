from __future__ import annotations

import dataclasses

from ablit.select import PlanOptions, apply_filter_chain, rank_offers
from helpers import OPTS, REQ, make_offer


def _names(result):
    return {r.name: r.removed for r in result.steps}


def test_steps_run_in_the_specified_order_and_count_removals():
    offers = [
        make_offer("ok"),
        make_offer("amd", gpu_vendor="amd"),
        make_offer("two", gpu_count=2),
        make_offer("small", gpu_vram_gb_each=16),
        make_offer("lowram", host_ram_gb=32),
        make_offer("nodisk", disk_gb=100),
        make_offer("slow", download_mbps=100),
        make_offer("spot", interruptible=True),
        make_offer("gone", rentable=False),
        make_offer("short", remaining_hours=1.0),
    ]
    result = apply_filter_chain(offers, REQ, OPTS)
    assert [s.name for s in result.steps] == [
        "engine floor", "disk floor", "download speed floor", "rental type and availability", "user filters"]
    assert [o.offer_id for o in result.kept] == ["ok"]
    assert _names(result) == {
        "engine floor": 4, "disk floor": 1, "download speed floor": 1,
        "rental type and availability": 3, "user filters": 0}


def test_compute_capability_floor():
    req = dataclasses.replace(REQ, compute_capability_min=7.5)
    offers = [make_offer("new", compute_capability=8.9), make_offer("old", compute_capability=6.1),
              make_offer("unknown", compute_capability=None)]
    kept = apply_filter_chain(offers, req, OPTS).kept
    assert [o.offer_id for o in kept] == ["new"]


def test_gpu_generation_must_be_one_the_engine_image_holds():
    req = dataclasses.replace(REQ, cuda_architectures=(86, 89, 120))
    offers = [make_offer("ada", compute_capability=8.9), make_offer("ampere", compute_capability=8.6),
              make_offer("blackwell", compute_capability=12.0), make_offer("a100", compute_capability=8.0),
              make_offer("hopper", compute_capability=9.0), make_offer("unknown", compute_capability=None)]
    result = apply_filter_chain(offers, req, OPTS)
    assert [o.offer_id for o in result.kept] == ["ada", "ampere", "blackwell"]
    reasons = result.steps[0].reasons
    assert reasons["GPU generation is not in the engine image"] == 2
    assert reasons["compute capability unknown"] == 1


def test_any_generation_passes_when_the_engine_lists_none():
    offers = [make_offer("a100", compute_capability=8.0), make_offer("unknown", compute_capability=None)]
    assert len(apply_filter_chain(offers, REQ, OPTS).kept) == 2


def test_total_vram_floor():
    req = dataclasses.replace(REQ, gpu_count=8, vram_gb_each=80, vram_gb_total=614)
    small = make_offer("small", gpu_count=8, gpu_vram_gb_each=40)
    big = make_offer("big", gpu_count=8, gpu_vram_gb_each=80)
    assert [o.offer_id for o in apply_filter_chain([small, big], req, OPTS).kept] == ["big"]


def test_gpu_count_must_match_exactly():
    req = dataclasses.replace(REQ, gpu_count=8, vram_gb_each=80)
    seven = make_offer("seven", gpu_count=7, gpu_vram_gb_each=80)
    nine = make_offer("nine", gpu_count=9, gpu_vram_gb_each=80)
    eight = make_offer("eight", gpu_count=8, gpu_vram_gb_each=80)
    assert [o.offer_id for o in apply_filter_chain([seven, nine, eight], req, OPTS).kept] == ["eight"]


def test_unadvertised_speed_is_kept_and_labeled():
    result = rank_offers([make_offer("u", download_mbps=None)], REQ, OPTS)
    assert result.ranked[0].speed_label == "not advertised, measured after boot"
    result = rank_offers([make_offer("a", download_mbps=900)], REQ, OPTS)
    assert result.ranked[0].speed_label == "advertised"


def test_spot_flag_allows_interruptible_offers():
    offers = [make_offer("spot", interruptible=True)]
    assert not apply_filter_chain(offers, REQ, OPTS).kept
    assert apply_filter_chain(offers, REQ, PlanOptions(spot=True)).kept


def test_remaining_rental_must_cover_startup_plus_hours():
    offers = [make_offer("a", remaining_hours=1.4), make_offer("b", remaining_hours=1.6)]
    kept = apply_filter_chain(offers, REQ, PlanOptions(hours=1.0)).kept
    assert [o.offer_id for o in kept] == ["b"]


def test_user_filters_apply_only_when_set():
    offers = [
        make_offer("v", verified=True, reliability=0.99, region="us", hourly=1.0),
        make_offer("u", verified=False, reliability=0.8, region="eu", hourly=3.0),
    ]
    assert len(apply_filter_chain(offers, REQ, OPTS).kept) == 2
    assert [o.offer_id for o in apply_filter_chain(offers, REQ, PlanOptions(trust="verified")).kept] == ["v"]
    assert [o.offer_id for o in apply_filter_chain(offers, REQ, PlanOptions(min_reliability=0.9)).kept] == ["v"]
    assert [o.offer_id for o in apply_filter_chain(offers, REQ, PlanOptions(region="eu")).kept] == ["u"]
    assert [o.offer_id for o in apply_filter_chain(offers, REQ, PlanOptions(max_price_hr=2.0)).kept] == ["v"]


def test_min_download_flag_overrides_the_profile_floor():
    offers = [make_offer("a", download_mbps=800)]
    assert apply_filter_chain(offers, REQ, OPTS).kept
    assert not apply_filter_chain(offers, REQ, PlanOptions(min_download_mbps=1000)).kept


def test_an_offer_that_may_have_no_public_ip_is_dropped_with_a_reason():
    result = apply_filter_chain([make_offer("a", ssh_reachable=False), make_offer("b")], REQ, OPTS)
    assert [o.offer_id for o in result.kept] == ["b"]
    reasons = {r for step in result.steps for r in step.reasons}
    assert "Community Cloud pods may have no public IP for SSH" in reasons
