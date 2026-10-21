"""The five tests that enforce the rule against steering by commission.

1. ablit.select and ablit.backends cannot import ablit.referral.
2. The planner gives the same filtered set, order and selection with referral
   IDs set for all marketplaces, for none, and with paying marketplaces shuffled.
3. A property test: the first-ranked offer has the lowest total among all
   offers that pass the filters, including sets larger than 64.
4. A cheaper backend without a referral program beats a more expensive backend
   with one.
5. Ties break on backend name, then offer ID, alphabetically.
"""

from __future__ import annotations

import ast
import dataclasses
import inspect
import random
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from ablit import referral
from ablit.select import (
    Offer,
    PlanOptions,
    apply_filter_chain,
    rank_offers,
    search,
    total_cost,
)
from ablit.select.rank import PRICE_RESOLUTION_DIGITS
from helpers import OPTS, REQ, make_offer, offer_sets

PAGE_SIZE = 64


# --- test 1: no imports of ablit.referral ----------------------------------

def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                continue
            names.add(base)
            names.update(f"{base}.{alias.name}" for alias in node.names)
    return names


@pytest.mark.parametrize("package", ["select", "backends"])
def test_no_module_imports_referral(cli_dir: Path, package: str):
    files = sorted((cli_dir / "ablit" / package).rglob("*.py"))
    assert files
    for file in files:
        offenders = [n for n in _imported_modules(file) if n == "ablit.referral" or n.startswith("ablit.referral.")]
        assert not offenders, f"{file} imports {offenders}"
        text = file.read_text(encoding="utf-8")
        assert "from ablit import referral" not in text
        assert "import ablit.referral" not in text


def test_importing_planner_and_backends_does_not_load_referral():
    code = "import ablit.select, ablit.backends, sys; assert 'ablit.referral' not in sys.modules"
    result = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_importlinter_contract_passes(repo_root: Path):
    tool = shutil.which("lint-imports")
    if tool is None:
        pytest.skip("lint-imports is not installed")
    result = subprocess.run([tool], cwd=repo_root, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr


def test_importlinter_contract_catches_a_violation(tmp_path: Path, repo_root: Path):
    tool = shutil.which("lint-imports")
    if tool is None:
        pytest.skip("lint-imports is not installed")
    package = tmp_path / "pkg"
    for name in ("select", "backends", "referral"):
        (package / name).mkdir(parents=True, exist_ok=True)
    for directory in (package, package / "select", package / "backends", package / "referral"):
        (directory / "__init__.py").write_text("")
    (package / "select" / "bad.py").write_text("from pkg import referral\n")
    # Reuse the real contract text with the package name swapped.
    contract = (repo_root / ".importlinter").read_text(encoding="utf-8")
    contract = contract.replace("ablit", "pkg")
    (tmp_path / ".importlinter").write_text(contract)
    env = {"PYTHONPATH": str(tmp_path), "PATH": "/usr/bin:/bin"}
    result = subprocess.run([tool], cwd=tmp_path, capture_output=True, text=True, env={**env})
    assert result.returncode != 0, result.stdout


def test_contract_file_names_both_packages(repo_root: Path):
    text = (repo_root / ".importlinter").read_text(encoding="utf-8")
    assert "ablit.select" in text and "ablit.backends" in text
    assert "forbidden_modules" in text and "ablit.referral" in text


def test_offers_and_planner_have_no_referral_fields():
    names = {f.name for f in dataclasses.fields(Offer)}
    assert not {n for n in names if "ref" in n.lower() or "commission" in n.lower()}
    for fn in (rank_offers, apply_filter_chain, search, total_cost):
        params = set(inspect.signature(fn).parameters)
        assert not {p for p in params if "ref" in p.lower() or "commission" in p.lower() or p == "ids"}
    option_fields = {f.name for f in dataclasses.fields(PlanOptions)}
    assert not {n for n in option_fields if "ref" in n.lower()}


# --- test 2: same result with and without referral IDs ---------------------

def _view(result) -> list[tuple[str, str, float]]:
    return [(r.offer.backend, r.offer.offer_id, r.total) for r in result.ranked]


def _mixed_offers() -> list[Offer]:
    offers = []
    for i in range(30):
        offers.append(make_offer(f"v{i:02d}", "vast", 1.0 + (i % 7) * 0.13, bandwidth_cost_per_gb=0.001 * (i % 3)))
        offers.append(make_offer(f"r{i:02d}", "runpod", 1.1 + (i % 5) * 0.11, download_mbps=None))
        offers.append(make_offer(f"x{i:02d}", "other", 1.2 + (i % 4) * 0.07))
    offers.append(make_offer("bad", "vast", 0.1, gpu_vram_gb_each=8))
    return offers


def test_same_plan_with_ids_set_unset_and_shuffled():
    offers = _mixed_offers()
    no_ids = referral.load_ids('[vast]\nid = ""\n[runpod]\nid = ""\n')
    all_ids = referral.load_ids('[vast]\nid = "VASTID1"\n[runpod]\nid = "RUNPODID1"\n')
    assert referral.disclosure("vast", all_ids) and referral.disclosure("vast", no_ids) is None

    baseline = rank_offers(offers, REQ, OPTS)
    with_ids = rank_offers(offers, REQ, OPTS)  # the planner cannot see the IDs, so this must match

    paying = [o for o in offers if o.backend in ("vast", "runpod")]
    others = [o for o in offers if o.backend not in ("vast", "runpod")]
    for seed in range(5):
        shuffled_paying = paying[:]
        random.Random(seed).shuffle(shuffled_paying)
        shuffled = shuffled_paying + others
        result = rank_offers(shuffled, REQ, OPTS)
        assert _view(result) == _view(baseline)
        assert [s.removed for s in result.steps] == [s.removed for s in baseline.steps]

    assert _view(with_ids) == _view(baseline)
    assert baseline.steps[0].removed == 1  # the offer with too little VRAM


@given(offer_sets(max_size=80), st.randoms(use_true_random=False))
@settings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.too_slow])
def test_order_does_not_depend_on_input_order(offers, rnd):
    shuffled = offers[:]
    rnd.shuffle(shuffled)
    assert _view(rank_offers(offers, REQ, OPTS)) == _view(rank_offers(shuffled, REQ, OPTS))


# --- test 3: the first-ranked offer has the lowest total -------------------

def _server(offers: list[Offer]):
    """Simulate a marketplace that returns at most 64 rows, cheapest hourly price first."""
    ordered = sorted(offers, key=lambda o: (o.hourly_all_in, o.offer_id))

    def fetch(after):
        rows = [o for o in ordered if after is None or o.hourly_all_in > after]
        return rows[:PAGE_SIZE]

    return fetch


def _brute_force_best(offers: list[Offer], opts: PlanOptions = OPTS):
    kept = apply_filter_chain(offers, REQ, opts).kept
    if not kept:
        return None
    return min(
        kept,
        key=lambda o: (round(total_cost(o, REQ, opts.hours), PRICE_RESOLUTION_DIGITS), o.backend, o.offer_id),
    )


def _run_search(offers: list[Offer], opts: PlanOptions = OPTS):
    return search(
        fetch_page=_server(offers),
        qualify=lambda page: apply_filter_chain(page, REQ, opts).kept,
        total_of=lambda o: total_cost(o, REQ, opts.hours),
        hours=opts.hours,
        startup_hours=REQ.startup_hours,
    )


@given(offer_sets(min_size=0, max_size=200))
@settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large])
def test_first_ranked_offer_has_the_lowest_total(offers):
    expected = _brute_force_best(offers)
    ranked = rank_offers(offers, REQ, OPTS).ranked
    outcome = _run_search(offers)
    assert outcome.complete  # at most 200 rows is 4 pages, under the page limit
    if expected is None:
        assert not ranked and outcome.best is None
    else:
        assert ranked[0].offer == expected
        assert outcome.best == expected


@st.composite
def needle_sets(draw):
    """More than 64 cheap-hourly offers with a costly transfer price, plus a pricier
    offer with a free transfer that is outside the first 64 rows by hourly price."""
    count = draw(st.integers(65, 150))
    start = draw(st.integers(100, 400))
    step = draw(st.integers(1, 5))
    high_transfer = draw(st.sampled_from([0.05, 0.1, 0.5]))
    offers = [
        make_offer(f"cheap-{i:03d}", draw(st.sampled_from(["vast", "runpod"])), (start + i * step) / 100.0,
                   bandwidth_cost_per_gb=high_transfer)
        for i in range(count)
    ]
    needle_cents = start + count * step + draw(st.integers(1, 200))
    offers.append(make_offer("needle", draw(st.sampled_from(["vast", "runpod", "other"])), needle_cents / 100.0,
                             bandwidth_cost_per_gb=0.0))
    return offers


@given(needle_sets())
@settings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large])
def test_cheap_transfer_offer_outside_the_first_64_is_found(offers):
    by_hourly = sorted(offers, key=lambda o: (o.hourly_all_in, o.offer_id))
    assert "needle" not in {o.offer_id for o in by_hourly[:PAGE_SIZE]}
    expected = _brute_force_best(offers)
    outcome = _run_search(offers)
    ranked = rank_offers(offers, REQ, OPTS).ranked
    assert outcome.complete
    assert outcome.pages >= 2  # the first page alone could not prove the answer
    assert outcome.best == expected == ranked[0].offer


def test_the_first_page_alone_would_have_missed_the_needle():
    cheap = [make_offer(f"cheap-{i:03d}", "vast", 1.0 + i * 0.01, bandwidth_cost_per_gb=0.1) for i in range(80)]
    needle = make_offer("needle", "runpod", 2.5, bandwidth_cost_per_gb=0.0)
    offers = cheap + [needle]
    first_page_best = min(_server(offers)(None), key=lambda o: total_cost(o, REQ, 1.0))
    assert first_page_best.offer_id != "needle"
    outcome = _run_search(offers)
    assert outcome.best.offer_id == "needle"
    assert outcome.pages >= 2


# --- test 4: a cheaper backend without a referral program wins -------------

def test_cheaper_backend_without_a_program_beats_a_paying_one():
    ids = referral.load_ids('[vast]\nid = "VASTID1"\n[runpod]\nid = "RUNPODID1"\n')
    assert ids["vast"] and ids["runpod"]
    offers = [
        make_offer("pay-1", "vast", 1.50),
        make_offer("pay-2", "runpod", 1.40),
        make_offer("free-1", "no-program-market", 1.30),
    ]
    ranked = rank_offers(offers, REQ, OPTS).ranked
    assert ranked[0].offer.backend == "no-program-market"
    # And the other way around, so the test also shows that price alone decides.
    offers = [
        make_offer("pay-1", "vast", 1.10),
        make_offer("free-1", "no-program-market", 1.30),
    ]
    assert rank_offers(offers, REQ, OPTS).ranked[0].offer.backend == "vast"


# --- test 5: ties break on backend, then offer ID --------------------------

@given(st.lists(st.tuples(st.sampled_from(["vast", "runpod", "alpha", "zeta"]), st.text("abc123", min_size=1, max_size=4)),
                min_size=2, max_size=20, unique=True))
@settings(max_examples=100, deadline=None)
def test_ties_break_on_backend_then_offer_id(pairs):
    offers = [make_offer(offer_id, backend, 1.25) for backend, offer_id in pairs]
    ranked = rank_offers(offers, REQ, OPTS).ranked
    keys = [(r.offer.backend, r.offer.offer_id) for r in ranked]
    assert keys == sorted(keys)
    assert len({round(r.total, 9) for r in ranked}) == 1


def test_a_tie_is_not_decided_by_input_order():
    a = make_offer("b-offer", "vast", 1.0)
    b = make_offer("a-offer", "vast", 1.0)
    c = make_offer("z-offer", "alpha", 1.0)
    for order in ([a, b, c], [c, b, a], [b, c, a]):
        ranked = rank_offers(order, REQ, OPTS).ranked
        assert [(r.offer.backend, r.offer.offer_id) for r in ranked] == [
            ("alpha", "z-offer"), ("vast", "a-offer"), ("vast", "b-offer")]
