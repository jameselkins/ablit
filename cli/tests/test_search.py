from __future__ import annotations

import pytest

from ablit.select import FilterRejected, lower_bound, search, total_cost
from ablit.select.search import MAX_PAGES
from helpers import REQ, make_offer


def _fetcher(offers, page_size=64, reject_after_first=False):
    ordered = sorted(offers, key=lambda o: (o.hourly_all_in, o.offer_id))
    calls = []

    def fetch(after):
        calls.append(after)
        if reject_after_first and after is not None:
            raise FilterRejected
        return [o for o in ordered if after is None or o.hourly_all_in > after][:page_size]

    fetch.calls = calls
    return fetch


def _search(fetch, hours=1.0, page_size=64, max_pages=MAX_PAGES):
    return search(
        fetch_page=fetch,
        qualify=lambda page: page,
        total_of=lambda o: total_cost(o, REQ, hours),
        hours=hours,
        startup_hours=REQ.startup_hours,
        page_size=page_size,
        max_pages=max_pages,
    )


def test_bound_is_last_price_times_hours_plus_startup():
    assert lower_bound(2.0, 3.0, 0.5) == pytest.approx(7.0)


def test_one_short_page_is_complete():
    offers = [make_offer(f"o{i}", hourly=1 + i * 0.1) for i in range(10)]
    fetch = _fetcher(offers)
    outcome = _search(fetch)
    assert outcome.complete and outcome.pages == 1 and outcome.checked == 10
    assert outcome.label is None
    assert fetch.calls == [None]


def test_stops_when_the_lowest_total_is_within_the_bound():
    # Free transfer and fast downloads: the cheapest row is provably the best, so
    # there is no second query even though the page was full.
    offers = [make_offer(f"o{i:03d}", hourly=1 + i * 0.5, download_mbps=1e9) for i in range(100)]
    fetch = _fetcher(offers)
    outcome = _search(fetch)
    assert outcome.complete
    assert outcome.pages == 1
    assert fetch.calls == [None]
    assert outcome.best.offer_id == "o000"


def test_keeps_going_when_the_bound_cannot_be_proved():
    cheap = [make_offer(f"c{i:03d}", hourly=1 + i * 0.01, bandwidth_cost_per_gb=0.1) for i in range(64)]
    far = [make_offer(f"f{i:03d}", hourly=5 + i * 0.01) for i in range(10)]
    fetch = _fetcher(cheap + far)
    outcome = _search(fetch)
    assert outcome.pages == 2
    assert fetch.calls[1] == pytest.approx(cheap[-1].hourly_all_in)
    assert outcome.best.offer_id.startswith("f")
    assert outcome.complete


def test_stops_after_ten_pages_and_labels_the_result():
    offers = [make_offer(f"o{i:04d}", hourly=1 + i * 0.001, bandwidth_cost_per_gb=1.0) for i in range(64 * 12)]
    fetch = _fetcher(offers)
    outcome = _search(fetch)
    assert outcome.pages == MAX_PAGES
    assert not outcome.complete
    assert outcome.checked == 64 * MAX_PAGES
    assert outcome.label == f"cheapest of the {64 * MAX_PAGES} offers checked"


def test_stops_when_the_marketplace_rejects_the_price_filter():
    offers = [make_offer(f"o{i:04d}", hourly=1 + i * 0.001, bandwidth_cost_per_gb=1.0) for i in range(200)]
    outcome = _search(_fetcher(offers, reject_after_first=True))
    assert outcome.pages == 1 and not outcome.complete
    assert outcome.label == "cheapest of the 64 offers checked"
    assert outcome.best is not None


def test_empty_result():
    outcome = _search(_fetcher([]))
    assert outcome.best is None and outcome.complete and outcome.checked == 0


def test_a_full_page_at_one_price_is_reported_as_incomplete():
    offers = [make_offer(f"o{i:03d}", hourly=2.0, bandwidth_cost_per_gb=1.0) for i in range(70)]
    ordered_page = sorted(offers, key=lambda o: o.offer_id)[:64]
    outcome = search(
        fetch_page=lambda after: ordered_page if after is None else [],
        qualify=lambda page: page,
        total_of=lambda o: total_cost(o, REQ, 1.0),
        hours=1.0,
        startup_hours=REQ.startup_hours,
    )
    assert not outcome.complete


def test_the_filter_chain_runs_on_each_page():
    offers = [make_offer(f"o{i:03d}", hourly=1 + i * 0.01, bandwidth_cost_per_gb=0.1,
                         gpu_vram_gb_each=8 if i < 64 else 24) for i in range(100)]
    from ablit.select import apply_filter_chain
    from helpers import OPTS

    outcome = search(
        fetch_page=_fetcher(offers),
        qualify=lambda page: apply_filter_chain(page, REQ, OPTS).kept,
        total_of=lambda o: total_cost(o, REQ, 1.0),
        hours=1.0,
        startup_hours=REQ.startup_hours,
    )
    # The first page has no qualifying offer, so the search must go on to the second.
    assert outcome.best is not None and outcome.best.offer_id == "o064"
