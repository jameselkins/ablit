from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from ablit.watchdog import ablit_watchdog as wd

CFG = wd.Config(
    started_at=1000.0,
    hourly_rate=3.6,  # one cent per ten seconds
    transfer_price_per_gb=0.02,
    cap_usd=10.0,
    advertised_mbps=800.0,
)


def test_spend_is_time_at_the_locked_rate_plus_bytes_received():
    assert wd.spend(CFG, 1000.0, 0) == 0
    assert wd.spend(CFG, 1000.0 + 3600, 0) == pytest.approx(3.6)
    assert wd.spend(CFG, 1000.0, 5_000_000_000) == pytest.approx(0.10)
    assert wd.spend(CFG, 1000.0 + 1800, 10_000_000_000) == pytest.approx(1.8 + 0.2)


def test_spend_ignores_bytes_counted_before_the_run_started():
    cfg = wd.Config(1000.0, 3.6, 0.02, 10.0, 800.0, baseline_rx_bytes=7_000_000_000)
    assert wd.spend(cfg, 1000.0, 7_000_000_000) == 0
    assert wd.spend(cfg, 1000.0, 6_000_000_000) == 0  # a counter reset never gives negative spend


def test_reserve_is_two_minutes_of_rate_plus_two_minutes_of_transfer():
    compute = 3.6 * 2 / 60
    transfer = 0.02 * (800e6 / 8) * 120 / 1e9
    assert wd.reserve(CFG) == pytest.approx(compute + transfer)


def test_continue_below_the_cap():
    state = wd.initial_state(1000.0, 0)
    decision = wd.decide(CFG, state, 1100.0, 1_000_000, downloading=True)
    assert decision.action == wd.CONTINUE


def test_destroy_when_spend_plus_reserve_reaches_the_cap():
    state = wd.initial_state(1000.0, 0)
    reserve = wd.reserve(CFG)
    # Find the time at which spend + reserve equals the cap exactly.
    seconds = (CFG.cap_usd - reserve) / CFG.hourly_rate * 3600
    just_before = wd.decide(CFG, state, 1000.0 + seconds - 1, 1, downloading=False)
    at_cap = wd.decide(CFG, state, 1000.0 + seconds + 0.5, 1, downloading=False)
    assert just_before.action == wd.CONTINUE
    assert at_cap.action == wd.DESTROY_CAP
    assert "cap" in at_cap.reason


def test_download_charges_count_toward_the_cap():
    state = wd.initial_state(1000.0, 0)
    # 500 GB at 0.02 per GB is 10 dollars, which alone reaches the cap.
    decision = wd.decide(CFG, state, 1001.0, 500_000_000_000, downloading=True)
    assert decision.action == wd.DESTROY_CAP


def test_destroy_after_ten_minutes_with_no_bytes_during_the_download():
    state = wd.initial_state(1000.0, 100)
    still = wd.decide(CFG, state, 1000.0 + 599, 100, downloading=True)
    assert still.action == wd.CONTINUE
    stalled = wd.decide(CFG, still.state, 1000.0 + 600, 100, downloading=True)
    assert stalled.action == wd.DESTROY_STALL


def test_no_stall_check_after_the_download_ends():
    state = wd.initial_state(1000.0, 100)
    decision = wd.decide(CFG, state, 1000.0 + 3000, 100, downloading=False)
    assert decision.action == wd.CONTINUE


def test_progress_resets_the_stall_clock():
    state = wd.initial_state(1000.0, 100)
    d1 = wd.decide(CFG, state, 1000.0 + 500, 200, downloading=True)
    assert d1.state.last_progress_at == 1500.0
    d2 = wd.decide(CFG, d1.state, 1000.0 + 1050, 200, downloading=True)
    assert d2.action == wd.CONTINUE  # 550 seconds since the last bytes
    d3 = wd.decide(CFG, d2.state, 1000.0 + 1100, 200, downloading=True)
    assert d3.action == wd.DESTROY_STALL


def test_seconds_until_cap():
    seconds = wd.seconds_until_cap(CFG, 1000.0, 0)
    assert seconds == pytest.approx((CFG.cap_usd - wd.reserve(CFG)) / CFG.hourly_rate * 3600)
    free = wd.Config(1000.0, 0.0, 0.0, 5.0, 100.0)
    assert wd.seconds_until_cap(free, 2000.0, 0) is None


def test_read_rx_bytes_skips_loopback(tmp_path: Path):
    sample = tmp_path / "dev"
    sample.write_text(
        "Inter-|   Receive                            |  Transmit\n"
        " face |bytes    packets errs drop fifo frame compressed multicast|bytes packets\n"
        "    lo: 999999    10    0    0    0     0          0         0   999999 10\n"
        "  eth0: 1500    10    0    0    0     0          0         0   600 5\n"
        "  eth1: 2500    10    0    0    0     0          0         0   600 5\n"
    )
    assert wd.read_rx_bytes(str(sample)) == 4000


def test_the_destroy_call_is_a_stub():
    with pytest.raises(NotImplementedError):
        wd.destroy_instance("test")


def test_load_config_reads_the_run_file(tmp_path: Path):
    path = tmp_path / "run.json"
    path.write_text(json.dumps({
        "run_id": "r1", "model_id": "m", "started_at": 5, "hourly_rate": 1.5,
        "transfer_price_per_gb": 0.01, "cap_usd": 7, "advertised_mbps": 900, "baseline_rx_bytes": 12}))
    cfg = wd._load_config(str(path))
    assert (cfg.started_at, cfg.hourly_rate, cfg.cap_usd, cfg.baseline_rx_bytes) == (5.0, 1.5, 7.0, 12)


def test_watchdog_uses_only_the_standard_library():
    tree = ast.parse(Path(wd.__file__).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        names = []
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names = [node.module]
        for name in names:
            assert name.split(".")[0] in sys.stdlib_module_names, name


@given(
    elapsed=st.floats(0, 1e6),
    more=st.floats(0, 1e6),
    rx=st.integers(0, 10**13),
    more_rx=st.integers(0, 10**13),
)
def test_spend_never_decreases(elapsed, more, rx, more_rx):
    assert wd.spend(CFG, 1000 + elapsed + more, rx + more_rx) >= wd.spend(CFG, 1000 + elapsed, rx) - 1e-9


@given(elapsed=st.floats(0, 1e5), rx=st.integers(0, 10**12), downloading=st.booleans())
def test_never_continues_once_spend_plus_reserve_reaches_the_cap(elapsed, rx, downloading):
    now = 1000.0 + elapsed
    decision = wd.decide(CFG, wd.initial_state(1000.0, 0), now, rx, downloading)
    if wd.spend(CFG, now, rx) + wd.reserve(CFG) >= CFG.cap_usd:
        assert decision.action == wd.DESTROY_CAP
    else:
        assert decision.action in (wd.CONTINUE, wd.DESTROY_STALL)
