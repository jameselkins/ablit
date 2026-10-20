from __future__ import annotations

import math

import pytest
from hypothesis import given
from hypothesis import strategies as st

from ablit.select import cost_breakdown, download_hours, total_cost
from ablit.select.cost import effective_speed
from helpers import REQ, make_offer


def test_download_hours_uses_decimal_gb_and_megabits():
    # 450 GB at 1000 Mbps is 450 * 8000 / (1000 * 3600) = 1 hour.
    assert download_hours(450, 1000) == pytest.approx(1.0)
    assert download_hours(0, 500) == 0
    with pytest.raises(ValueError):
        download_hours(10, 0)


def test_total_cost_matches_the_formula_by_hand():
    offer = make_offer("o", "vast", hourly=2.0, bandwidth_cost_per_gb=0.01, fixed_fees=0.25, download_mbps=1000)
    req = REQ  # download_gb 370, image_gb 0, startup 0.5
    d = 370 * 8000 / (1000 * 3600)
    expected = 2.0 * (d + 0.5 + 3.0) + 0.01 * 370 + 0.25
    assert total_cost(offer, req, 3.0) == pytest.approx(expected)


def test_image_size_counts_toward_transfer_only():
    import dataclasses

    req = dataclasses.replace(REQ, image_gb=10.0)
    offer = make_offer("o", hourly=1.0, bandwidth_cost_per_gb=0.02)
    base = total_cost(offer, REQ, 1.0)
    assert total_cost(offer, req, 1.0) == pytest.approx(base + 0.02 * 10.0)


def test_an_offer_with_no_advertised_speed_ranks_at_the_floor():
    unknown = make_offer("u", hourly=1.0, download_mbps=None)
    at_floor = make_offer("f", hourly=1.0, download_mbps=REQ.min_download_mbps)
    assert effective_speed(unknown, REQ.min_download_mbps) == REQ.min_download_mbps
    assert total_cost(unknown, REQ, 1.0) == pytest.approx(total_cost(at_floor, REQ, 1.0))
    faster = make_offer("g", hourly=1.0, download_mbps=2000)
    assert total_cost(faster, REQ, 1.0) < total_cost(unknown, REQ, 1.0)


def test_breakdown_shows_download_and_startup_separately():
    parts = cost_breakdown(make_offer("o", hourly=1.0, download_mbps=1000), REQ, 2.0)
    assert parts.startup_hours == REQ.startup_hours
    assert parts.download_hours == pytest.approx(370 * 8000 / (1000 * 3600))
    assert parts.total == pytest.approx(parts.compute + parts.transfer + parts.fixed)


@given(
    hourly=st.floats(0.01, 50),
    hours=st.floats(0, 100),
    extra=st.floats(0, 100),
    per_gb=st.floats(0, 1),
    speed=st.floats(10, 10000),
)
def test_total_grows_with_hours_and_never_goes_below_the_bound(hourly, hours, extra, per_gb, speed):
    offer = make_offer("o", hourly=hourly, bandwidth_cost_per_gb=per_gb, download_mbps=speed)
    low = total_cost(offer, REQ, hours)
    high = total_cost(offer, REQ, hours + extra)
    assert high >= low - 1e-9
    # The bound the search rule relies on: total(H) >= hourly * (H + S).
    assert low >= hourly * (hours + REQ.startup_hours) - 1e-9
    assert math.isfinite(low)
