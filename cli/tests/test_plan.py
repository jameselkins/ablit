"""`ablit plan` end to end, with the HTTP calls answered by invented responses."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
import respx

from ablit import exitcodes
from ablit.backends import RunPodBackend, VastBackend
from ablit.catalog import load_catalog, resolve
from ablit.cli import main
from ablit.notices import UNTESTED_NOTICE
from ablit.plan import JSON_SCHEMA_VERSION
from ablit.select import PlanOptions, rank_offers, total_cost
from ablit.select.request import request_from_entry
from market_fixtures import (
    RUNPOD_URL,
    VAST_URL,
    FakeRunPod,
    FakeVast,
    runpod_type,
    vast_row,
)

MODEL = "llama31-8b-abliterated-q4km"  # one 8 GB GPU, 16 GB host RAM, a 4.9 GB download
ALLOCATED = 5  # the entry's disk need rounded up


def vast_rows(count: int = 6, start_base: float = 0.40) -> list[dict]:
    return [vast_row(9100 + i, base=start_base + i * 0.07, allocated=ALLOCATED) for i in range(count)]


def runpod_types() -> list[dict]:
    return [
        runpod_type("NVIDIA GeForce RTX 3090", "RTX 3090", 24, price=0.35, pod_ram=100),
        runpod_type("NVIDIA RTX A4000", "RTX A4000", 16, price=0.55, pod_ram=40),
        runpod_type("NVIDIA GeForce RTX 3070", "RTX 3070", 8, price=0.30, pod_ram=24),
    ]


def run_plan(capsys, *argv, vast=None, runpod=None, environ=None, backends=None, expect_exit=None):
    """Run `ablit plan` with both endpoints answered by the given callables."""
    router = respx.mock(assert_all_called=False)
    if vast is not None:
        router.post(VAST_URL).mock(side_effect=vast)
    if runpod is not None:
        router.post(RUNPOD_URL).mock(side_effect=runpod)
    with router:
        client = httpx.Client()
        available = backends or {"runpod": RunPodBackend(client), "vast": VastBackend(client)}
        code = main(["plan", MODEL, *argv], backends=available, environ=environ or {})
    captured = capsys.readouterr()
    if expect_exit is not None:
        assert code == expect_exit, captured.err + captured.out
    return code, captured.out, captured.err


# --- the report ----------------------------------------------------------


def test_text_report_has_every_part(capsys):
    code, out, err = run_plan(capsys, "--hours", "3", "--max-spend", "20",
                              vast=FakeVast(vast_rows()), runpod=FakeRunPod(runpod_types()))
    assert code == 0, err
    assert f"Plan for {MODEL} (status: unverified)" in out
    assert UNTESTED_NOTICE in out
    assert "Chosen offer" in out and "Offer 2" in out and "Offer 5" in out and "Offer 6" not in out
    for label in ("1 hour", "24 hours", "7 days", "--hours (3)"):
        assert label in out
    for field_name in ("Offer:", "GPU:", "Host RAM:", "Disk:", "Download:", "Host:", "Price:", "Transfer:", "One-time:"):
        assert field_name in out
    assert "GPU only:" in out
    assert "speed not advertised, measured after boot" in out  # RunPod offers
    assert "Mbps advertised" in out  # Vast offers
    assert "Filter steps, over the" in out and "engine floor" in out and "disk floor" in out
    assert "download speed floor" in out and "rental type and availability" in out and "user filters" in out
    assert "Backends" in out and "vast:" in out and "runpod:" in out
    assert "How the spend cap works: a watchdog on the rented machine destroys it when" in out
    assert "$20.00" in out
    assert "ablit plan rents nothing." in out
    assert "cheapest of the" not in out  # both searches were complete


def test_the_chosen_offer_is_the_one_with_the_lowest_total(capsys):
    code, out, err = run_plan(capsys, "--json", vast=FakeVast(vast_rows()), runpod=FakeRunPod(runpod_types()))
    data = json.loads(out)
    entry = resolve(load_catalog().entries, MODEL)
    req = request_from_entry(entry)
    chosen = data["chosen"]
    totals = [data["chosen"]["ranking_total_usd"]] + [n["ranking_total_usd"] for n in data["next"]]
    assert totals == sorted(totals)
    assert len(data["next"]) == 4
    assert data["qualified_count"] >= 5
    # The cheapest offer by hourly price here is the 3070 on RunPod, which has too little host RAM,
    # so the chosen offer must be one that passed the filters.
    assert chosen["gpu_vram_gb_each"] >= 8 and chosen["host_ram_gb"] >= 16
    hourly = chosen["hourly_all_in_usd"]
    assert chosen["totals"][0]["total"] == pytest.approx(
        hourly * (chosen["download_hours"] + chosen["startup_hours"] + 1) + chosen["transfer_usd_per_gb"] * req.download_gb
    )


def test_ranking_matches_the_rank_module_exactly(capsys):
    vast = FakeVast(vast_rows())
    code, out, err = run_plan(capsys, "--json", vast=vast, runpod=FakeRunPod(runpod_types()))
    data = json.loads(out)
    router = respx.mock(assert_all_called=False)
    router.post(VAST_URL).mock(side_effect=FakeVast(vast_rows()))
    router.post(RUNPOD_URL).mock(side_effect=FakeRunPod(runpod_types()))
    entry = resolve(load_catalog().entries, MODEL)
    req = request_from_entry(entry)
    with router:
        client = httpx.Client()
        offers = []
        for backend in (RunPodBackend(client), VastBackend(client)):
            offers += backend.search(req, None, PlanOptions()).offers
    expected = rank_offers(offers, req, PlanOptions()).ranked
    got = [(data["chosen"]["backend"], data["chosen"]["offer_id"])] + [(n["backend"], n["offer_id"]) for n in data["next"]]
    assert got == [(r.offer.backend, r.offer.offer_id) for r in expected[:5]]


def test_json_shape(capsys):
    code, out, err = run_plan(capsys, "--json", "--hours", "2", "--max-spend", "9",
                              vast=FakeVast(vast_rows()), runpod=FakeRunPod(runpod_types()))
    assert code == 0, err
    data = json.loads(out)
    assert data["schema"] == JSON_SCHEMA_VERSION == 1
    assert data["command"] == "plan" and data["result"] == "ok"
    assert set(data) >= {
        "schema", "command", "checked_at", "result", "complete", "incomplete_label", "model", "request", "options",
        "chosen", "next", "qualified_count", "checked_count", "filter_steps", "backends", "notices",
    }
    assert data["model"]["id"] == MODEL and data["model"]["status"] == "unverified"
    assert data["model"]["revision"] and data["model"]["engine"] == "llamacpp"
    assert "image_digest" in data["model"]
    assert data["options"]["hours"] == 2 and data["options"]["max_spend"] == 9
    offer = data["chosen"]
    assert set(offer) >= {
        "rank", "backend", "offer_id", "gpu_model", "gpu_count", "gpu_vram_gb_each", "host_ram_gb", "disk_allocated_gb",
        "download_mbps", "verified", "remaining_hours", "hourly_all_in_usd", "gpu_only_hourly_usd",
        "transfer_usd_per_gb", "one_time_fees_usd", "totals", "cap",
    }
    assert [t["label"] for t in offer["totals"]] == ["1 hour", "24 hours", "7 days", "--hours (2)"]
    assert [t["hours"] for t in offer["totals"]] == [1, 24, 168, 2]
    assert offer["cap"]["cap_usd"] == 9
    assert [s["name"] for s in data["filter_steps"]] == [
        "engine floor", "disk floor", "download speed floor", "rental type and availability", "user filters"
    ]
    assert {b["name"] for b in data["backends"]} == {"vast", "runpod"}
    assert all(b["status"] == "ok" for b in data["backends"])
    assert "How the spend cap works" in data["notices"]["spend_cap"]
    assert data["notices"]["untested"] == UNTESTED_NOTICE
    assert out.strip().startswith("{") and out.strip().endswith("}")  # JSON only, nothing else on stdout


def test_a_cap_notice_without_a_cap_does_not_print_a_placeholder(capsys):
    code, out, err = run_plan(capsys, vast=FakeVast(vast_rows()), runpod=FakeRunPod(runpod_types()))
    assert "$<cap>" not in out
    assert "your --max-spend cap" in out


# --- an empty result -----------------------------------------------------


def test_empty_result_explains_itself_with_filter_counts_and_exits_3(capsys):
    # Every row is too weak for the profile in one way or another.
    rows = [
        vast_row(9201, base=0.4, allocated=ALLOCATED, cpu_ram=8000),          # not enough host RAM
        vast_row(9202, base=0.5, allocated=ALLOCATED, gpu_ram=4096, gpu_total_ram=4096),  # not enough VRAM
        vast_row(9203, base=0.6, allocated=ALLOCATED, inet_down=100.0),       # download too slow
    ]
    types = [runpod_type("NVIDIA GeForce RTX 3070", "RTX 3070", 6, price=0.3, pod_ram=24)]  # 6 GB, too little
    code, out, err = run_plan(capsys, vast=FakeVast(rows), runpod=FakeRunPod(types))
    assert code == exitcodes.NOT_SUPPORTED == 3
    assert "No offer qualified." in out
    assert "engine floor: " in out and "removed because: not enough VRAM per GPU" in out
    assert "removed because: not enough host RAM" in out
    assert "download speed floor: " in out and "removed because: download speed below the floor" in out
    assert "Chosen offer" not in out
    # The engine floor saw four rows: three from Vast and one from RunPod, whose two tiers gave the same price.
    data = json.loads(run_plan(capsys, "--json", vast=FakeVast(rows), runpod=FakeRunPod(types))[1])
    assert data["result"] == "no_offer" and data["chosen"] is None and data["next"] == []
    steps = {step["name"]: step for step in data["filter_steps"]}
    assert steps["engine floor"]["before"] == 4 and steps["engine floor"]["removed"] == 3
    assert steps["download speed floor"]["before"] == 1 and steps["download speed floor"]["removed"] == 1


def test_empty_result_when_no_backend_returns_anything(capsys):
    code, out, err = run_plan(capsys, vast=FakeVast([]), runpod=FakeRunPod([]))
    assert code == 3
    assert "No offers were returned, so no filter step ran." in out


# --- backends that fail, time out or are left out --------------------------


def test_a_backend_timeout_leaves_the_other_result_in_place_and_names_the_failed_one(capsys):
    def slow(request: httpx.Request):
        raise httpx.ReadTimeout("simulated timeout", request=request)

    code, out, err = run_plan(capsys, vast=slow, runpod=FakeRunPod(runpod_types()))
    assert code == 0, err
    assert "Chosen offer" in out
    assert "vast: timed out." in out
    assert "The comparison did not include vast." in out
    assert "runpod: " in out and "qualified" in out
    data = json.loads(run_plan(capsys, "--json", vast=slow, runpod=FakeRunPod(runpod_types()))[1])
    statuses = {b["name"]: b for b in data["backends"]}
    assert statuses["vast"]["status"] == "timed_out" and "timed out" in statuses["vast"]["reason"]
    assert statuses["runpod"]["status"] == "ok"
    assert data["chosen"]["backend"] == "runpod"


def test_when_every_backend_fails_the_exit_code_is_3_and_both_are_named(capsys):
    def down(request: httpx.Request):
        raise httpx.ConnectError("simulated outage", request=request)

    code, out, err = run_plan(capsys, vast=down, runpod=down)
    assert code == 3
    assert "vast: failed." in out and "runpod: failed." in out
    assert "The comparison did not include vast." in out and "The comparison did not include runpod." in out


def test_an_unrecognized_response_fails_that_backend_only(capsys):
    rows = [vast_row(9301, num_gpus="one")]
    code, out, err = run_plan(capsys, vast=FakeVast(rows), runpod=FakeRunPod(runpod_types()))
    assert code == 0, err
    assert "vast: failed." in out and "num_gpus" in out
    assert "Chosen offer" in out


def test_a_rejected_key_fails_that_backend_only(capsys):
    code, out, err = run_plan(
        capsys, vast=lambda request: httpx.Response(401, json={"error": "unauthorized"}),
        runpod=FakeRunPod(runpod_types()), environ={"VAST_API_KEY": "invented-test-key"},
    )
    assert code == 0, err
    assert "vast: failed." in out and "API key was refused" in out
    assert "invented-test-key" not in out + err


def test_backends_flag_limits_the_search_and_lists_what_was_left_out(capsys):
    vast = FakeVast(vast_rows())
    code, out, err = run_plan(capsys, "--backends", "vast", vast=vast, runpod=FakeRunPod(runpod_types()))
    assert code == 0, err
    assert "runpod: left out because it was not named in --backends." in out
    assert vast.bodies  # Vast was searched
    data = json.loads(run_plan(capsys, "--json", "--backends", "vast", vast=FakeVast(vast_rows()))[1])
    assert {b["name"]: b["status"] for b in data["backends"]} == {"vast": "ok", "runpod": "left_out"}
    assert all(offer["backend"] == "vast" for offer in [data["chosen"], *data["next"]])


def test_unknown_backend_is_a_usage_error(capsys):
    code, out, err = run_plan(capsys, "--backends", "vast,clore", vast=FakeVast([]))
    assert code == exitcodes.USAGE
    assert "unknown backend clore" in err


def test_keys_come_from_the_environment_when_set(capsys):
    vast = FakeVast(vast_rows())
    runpod = FakeRunPod(runpod_types())
    run_plan(capsys, vast=vast, runpod=runpod, environ={"VAST_API_KEY": "invented-vast-key"})
    assert vast.headers[0]["authorization"] == "Bearer invented-vast-key"
    assert "authorization" not in runpod.headers[0]
    run_plan(capsys, vast=vast, runpod=runpod, environ={"RUNPOD_API_KEY": "invented-runpod-key"})
    assert runpod.headers[-1]["authorization"] == "Bearer invented-runpod-key"


# --- incomplete searches -------------------------------------------------


def test_incomplete_search_is_labeled_cheapest_of_n_offers_checked(capsys):
    rows = [vast_row(9400 + i, base=1.0 + i * 0.01, allocated=ALLOCATED, inet_down_cost=0.9,
                     internet_down_cost_per_tb=None) for i in range(150)]
    for row in rows:
        row.pop("internet_down_cost_per_tb")
    code, out, err = run_plan(capsys, "--backends", "vast", vast=FakeVast(rows, reject_cursor=True))
    assert code == 0, err
    match = [line for line in out.splitlines() if line.startswith("Result: cheapest of the ")]
    assert match and match[0].endswith("offers checked.")
    data = json.loads(run_plan(capsys, "--json", "--backends", "vast", vast=FakeVast(rows, reject_cursor=True))[1])
    assert data["complete"] is False
    assert data["incomplete_label"] == f"cheapest of the {data['checked_count']} offers checked"
    assert [b["complete"] for b in data["backends"] if b["name"] == "vast"] == [False]


# --- flags ---------------------------------------------------------------


def test_spot_is_refused_until_a_round_trip_has_been_tested(capsys):
    code, out, err = run_plan(capsys, "--spot", vast=FakeVast([]), runpod=FakeRunPod([]))
    assert code == exitcodes.USAGE
    assert "--spot is disabled" in err


@pytest.mark.parametrize("flag,value", [("--hours", "0"), ("--min-download", "-5"), ("--max-spend", "0"),
                                        ("--max-price-hr", "0"), ("--min-reliability", "2")])
def test_bad_numbers_are_usage_errors(capsys, flag, value):
    code, out, err = run_plan(capsys, flag, value, vast=FakeVast([]), runpod=FakeRunPod([]))
    assert code == exitcodes.USAGE and flag in err


def test_user_filters_reach_the_server_and_the_chain(capsys):
    vast = FakeVast(vast_rows())
    run_plan(capsys, "--trust", "verified", "--region", "us", "--min-reliability", "0.9", "--max-price-hr", "5",
             "--min-download", "700", "--backends", "vast", vast=vast)
    body = vast.bodies[0]
    assert body["verified"] == {"eq": True} and body["geolocation"] == {"in": ["US"]}
    assert body["reliability"] == {"gte": 0.9} and body["inet_down"] == {"gte": 700}
    assert body["dph_base"] == {"lte": 5.0}


def test_up_is_still_not_implemented(capsys):
    code = main(["up", MODEL, "--max-spend", "5"], backends={})
    assert code == 2


# --- referral code is never involved -------------------------------------


def test_plan_does_not_import_or_call_referral_code(capsys, monkeypatch):
    import ablit

    # Make any attempt to import the package fail loudly while the plan runs.
    monkeypatch.setitem(sys.modules, "ablit.referral", None)
    monkeypatch.delattr(ablit, "referral", raising=False)
    code, out, err = run_plan(capsys, "--json", vast=FakeVast(vast_rows()), runpod=FakeRunPod(runpod_types()))
    assert code == 0, err
    markers = ("ref_" + "id=", "?" + "ref=", "referral")  # built in parts so the link scan in test_referral skips this line
    assert not [m for m in markers if m in out.lower()]


def test_plan_modules_do_not_import_referral(cli_dir: Path):
    for name in ("plan.py", "plan_text.py", "cli.py"):
        text = (cli_dir / "ablit" / name).read_text(encoding="utf-8")
        assert not re.search(r"^\s*(from|import)\s+ablit(\.referral|\s+import\s+.*referral)", text, re.M), name


def test_a_fresh_interpreter_does_not_load_referral_when_planning(cli_dir: Path):
    script = (
        "import sys, httpx, respx\n"
        "sys.path.insert(0, %r)\n"
        "from market_fixtures import *\n"
        "from ablit.cli import main\n"
        "from ablit.backends import VastBackend, RunPodBackend\n"
        "router = respx.mock(assert_all_called=False)\n"
        "router.post(VAST_URL).mock(side_effect=FakeVast([vast_row(9500, allocated=5)]))\n"
        "router.post(RUNPOD_URL).mock(side_effect=FakeRunPod([]))\n"
        "with router:\n"
        "    c = httpx.Client()\n"
        "    code = main(['plan', 'llama31-8b-abliterated-q4km'], backends={'vast': VastBackend(c), 'runpod': RunPodBackend(c)}, environ={})\n"
        "assert code == 0, code\n"
        "assert 'ablit.referral' not in sys.modules, 'referral was imported'\n"
    ) % str(cli_dir / "tests")
    result = subprocess.run([sys.executable, "-I", "-c", script], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr


def test_plan_prints_the_unknown_compute_capability_reason_in_the_filter_counts(capsys):
    # The Xolotl entry needs compute capability 7.5. One type is in the table and one is not.
    types = [
        runpod_type("NVIDIA GeForce RTX 4090", "RTX 4090", 24, price=0.6, pod_ram=120),
        runpod_type("NVIDIA Invented Accelerator", "Invented", 24, price=0.4, pod_ram=120),
    ]
    router = respx.mock(assert_all_called=False)
    router.post(RUNPOD_URL).mock(side_effect=FakeRunPod(types))
    with router:
        available = {"runpod": RunPodBackend(httpx.Client())}
        code = main(["plan", "ds41-1017-xq-v3-uncensored", "--backends", "runpod"], backends=available, environ={})
    out = capsys.readouterr().out
    assert code == 0
    assert "removed because: compute capability unknown" in out
    assert "RTX 4090" in out and "Invented" not in out.split("Filter steps")[0]
