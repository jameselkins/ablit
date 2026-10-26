"""--gpu and --exclude-gpu: parsing, matching across marketplace spellings, filter steps and the empty result."""

from __future__ import annotations

import dataclasses
import json

import httpx
import pytest
import respx

from ablit import exitcodes
from ablit.cli import build_parser, main
from ablit.runs import RunStore
from ablit.select import PlanOptions, apply_filter_chain
from ablit.select.gpu_names import gpu_matches, gpu_name_words, parse_gpu_names
from helpers import OPTS, REQ, make_offer
from market_fixtures import RUNPOD_URL, FakeRunPod, FakeVast, runpod_type
from test_plan import ALLOCATED, run_plan, vast_row
from test_runpod_lifecycle import GPU, PODS
from test_runpod_up import launch_environment, pod, run_pod

# --- parsing ---------------------------------------------------------------


def test_names_split_on_commas_and_repeat_without_blanks_or_duplicates():
    assert parse_gpu_names(["5090, RTX PRO 6000", "a40,", "5090"]) == ("5090", "RTX PRO 6000", "a40")
    assert parse_gpu_names(None) == ()


def test_a_name_with_nothing_to_compare_is_refused():
    with pytest.raises(ValueError):
        parse_gpu_names(["NVIDIA GeForce"])


def test_both_commands_accept_the_flags_repeated():
    for command in ("plan", "up"):
        args = build_parser().parse_args([command, "m", "--gpu", "5090", "--gpu", "a40,h100", "--exclude-gpu", "v100"])
        assert args.gpu == ["5090", "a40,h100"] and args.exclude_gpu == ["v100"]


@pytest.mark.parametrize("flags", [["--gpu", ""], ["--gpu", " , "], ["--exclude-gpu", ","], ["--gpu", "nvidia"]])
def test_an_empty_or_vendor_only_name_is_a_usage_error(capsys, flags):
    code, out, err = run_plan(capsys, *flags, runpod=FakeRunPod([]), vast=FakeVast([]))
    assert code == exitcodes.USAGE
    assert "GPU model name" in err


# --- matching --------------------------------------------------------------


@pytest.mark.parametrize("name", ["5090", "rtx5090", "RTX 5090", "GeForce RTX 5090", "NVIDIA GeForce RTX 5090"])
@pytest.mark.parametrize("model", ["RTX 5090", "NVIDIA GeForce RTX 5090", "GeForce RTX 5090", "rtx_5090"])
def test_every_spelling_of_the_5090_matches(name, model):
    assert gpu_matches(model, [name])


@pytest.mark.parametrize("model", [
    "RTX PRO 6000 WS",                                   # Vast
    "RTX PRO 6000 S",                                    # Vast
    "RTX PRO 6000 Blackwell Workstation Edition",        # RunPod
    "RTX PRO 6000 Blackwell Server Edit",                # Clore, cut short
    "RTX PRO 6000 Blackwell Server Edition",             # Lium
    "RTX PRO 6000 Blackwell",
])
def test_rtx_pro_6000_matches_every_edition(model):
    assert gpu_matches(model, ["RTX PRO 6000"])
    assert gpu_matches(model, ["rtx pro 6000 blackwell"])


def test_editions_are_told_apart_when_named():
    assert gpu_matches("RTX PRO 6000 WS", ["RTX PRO 6000 Workstation"])
    assert gpu_matches("RTX PRO 6000 Blackwell Workstation Edition", ["rtx pro 6000 ws"])
    assert not gpu_matches("RTX PRO 6000 S", ["RTX PRO 6000 Workstation"])
    assert gpu_matches("RTX PRO 6000 S", ["RTX PRO 6000 Server"])
    assert gpu_matches("RTX PRO 6000 Blackwell Max-Q Workstation Edition", ["RTX PRO 6000 MaxQ"])


def test_whole_words_only():
    assert not gpu_matches("RTX 6000 Ada Generation", ["RTX PRO 6000"])
    assert not gpu_matches("RTX PRO 6000 WS", ["RTX 6000"])
    assert not gpu_matches("A100 80GB PCIe", ["A10"])
    assert not gpu_matches("RTX 5080", ["5090"])
    assert gpu_matches("Tesla V100SXM216GB", ["V100"])      # Clore
    assert gpu_matches("Tesla V100-SXM2-16GB", ["v100"])    # RunPod
    assert gpu_matches("L40S", ["L40"]) and not gpu_matches("L40", ["L40S"])
    assert gpu_name_words("NVIDIA GeForce RTX 5090") == ("rtx", "5090")


# --- the filter step -------------------------------------------------------


def _offers():
    return [
        make_offer("v100", gpu_model="Tesla V100", hourly=0.2),
        make_offer("a40", gpu_model="A40", hourly=0.3),
        make_offer("5090", gpu_model="RTX 5090", hourly=0.6),
    ]


def test_gpu_filter_removes_offers_in_the_user_filters_step_with_a_reason():
    opts = dataclasses.replace(OPTS, gpu=("rtx5090",))
    result = apply_filter_chain(_offers(), REQ, opts)
    assert [o.offer_id for o in result.kept] == ["5090"]
    step = result.steps[-1]
    assert step.name == "user filters" and step.removed == 2
    assert step.reasons == {"GPU model not named in --gpu": 2}


def test_exclude_gpu_removes_only_the_named_model():
    opts = dataclasses.replace(OPTS, exclude_gpu=("A40",))
    result = apply_filter_chain(_offers(), REQ, opts)
    assert [o.offer_id for o in result.kept] == ["v100", "5090"]
    assert result.steps[-1].reasons == {"GPU model named in --exclude-gpu": 1}


def test_no_gpu_flags_remove_nothing():
    assert len(apply_filter_chain(_offers(), REQ, PlanOptions()).kept) == 3


# --- plan end to end -------------------------------------------------------


def _vast_rows():
    return [
        vast_row(9301, base=0.40, allocated=ALLOCATED, gpu_name="Tesla V100"),
        vast_row(9302, base=0.50, allocated=ALLOCATED, gpu_name="A40"),
        vast_row(9303, base=0.90, allocated=ALLOCATED, gpu_name="RTX 5090"),
    ]


def test_plan_json_picks_the_named_gpu_and_explains_the_rest(capsys):
    code, out, err = run_plan(capsys, "--json", "--gpu", "5090", runpod=FakeRunPod([]), vast=FakeVast(_vast_rows()),
                              expect_exit=0)
    data = json.loads(out)
    assert data["schema"] == 1
    assert data["chosen"]["gpu_model"] == "RTX 5090"
    assert data["options"]["gpu"] == ["5090"] and data["options"]["exclude_gpu"] == []
    assert data["qualified_gpu_models"] == ["A40", "RTX 5090", "Tesla V100"]
    steps = {s["name"]: s for s in data["filter_steps"]}
    assert steps["user filters"]["reasons"] == {"GPU model not named in --gpu": 2}
    vast = next(b for b in data["backends"] if b["name"] == "vast")
    assert {s["name"]: s for s in vast["filter_steps"]}["user filters"]["removed"] == 2


def test_plan_text_names_the_gpu_flags(capsys):
    code, out, err = run_plan(capsys, "--gpu", "a40", "--exclude-gpu", "5090", runpod=FakeRunPod([]), vast=FakeVast(_vast_rows()),
                              expect_exit=0)
    assert "a GPU model that matches --gpu: a40" in out
    assert "a GPU model that does not match --exclude-gpu: 5090" in out
    assert "removed because: GPU model not named in --gpu" in out


def test_plan_with_no_matching_gpu_exits_3_and_lists_the_models_that_qualified(capsys):
    code, out, err = run_plan(capsys, "--gpu", "H100", runpod=FakeRunPod([]), vast=FakeVast(_vast_rows()),
                              expect_exit=exitcodes.NOT_SUPPORTED)
    assert "No offer qualified." in out
    assert "No offer qualified with the GPU models that --gpu allow" in err
    assert "A40, RTX 5090, Tesla V100" in err
    data = json.loads(run_plan(capsys, "--json", "--gpu", "H100", runpod=FakeRunPod([]), vast=FakeVast(_vast_rows()))[1])
    assert data["result"] == "no_offer" and data["qualified_gpu_models"] == ["A40", "RTX 5090", "Tesla V100"]


def test_an_empty_result_from_other_filters_does_not_blame_the_gpu_flag(capsys):
    rows = [vast_row(9401, base=0.4, allocated=ALLOCATED, cpu_ram=8000, gpu_name="RTX 5090")]
    code, out, err = run_plan(capsys, "--gpu", "5090", runpod=FakeRunPod([]), vast=FakeVast(rows), expect_exit=exitcodes.NOT_SUPPORTED)
    assert "--gpu" not in err


# --- up ----------------------------------------------------------------------


def _runpod_types():
    return [
        runpod_type("NVIDIA A40", "A40", 48, price=0.10, pod_ram=100),
        runpod_type(GPU, "RTX 4090", 24, price=0.20, pod_ram=100),
    ]


def test_up_applies_exclude_gpu_before_quoting(monkeypatch, capsys):
    launch_environment(monkeypatch)
    with respx.mock(assert_all_called=False) as router:
        router.post(RUNPOD_URL).mock(side_effect=FakeRunPod(_runpod_types()))
        create = router.post(PODS).mock(return_value=httpx.Response(201, json=pod(disk=600, cost=0.20)))
        router.get(PODS).mock(side_effect=run_pod)
        code = main(["up", "llama31-8b-abliterated-q4km", "--untested", "--max-spend", "5", "--backends", "runpod",
                     "--exclude-gpu", "a40"], environ={"RUNPOD_API_KEY": "invented-runpod-key"})
        assert code == 0 and create.call_count == 1
    assert json.loads(create.calls[0].request.content)["gpu"]["id"] == GPU
    record = RunStore().all()[0]
    assert record["quote"]["options"]["exclude_gpu"] == ["a40"]
    assert record["quote"]["chosen"]["gpu_model"] == "RTX 4090"


def test_up_with_no_matching_gpu_exits_3_without_renting(monkeypatch, capsys):
    launch_environment(monkeypatch)
    with respx.mock(assert_all_called=False) as router:
        router.post(RUNPOD_URL).mock(side_effect=FakeRunPod(_runpod_types()))
        create = router.post(PODS).mock(return_value=httpx.Response(201, json=pod(disk=600, cost=0.20)))
        code = main(["up", "llama31-8b-abliterated-q4km", "--untested", "--max-spend", "5", "--backends", "runpod",
                     "--gpu", "5090"], environ={"RUNPOD_API_KEY": "invented-runpod-key"})
        assert code == exitcodes.NOT_SUPPORTED and create.call_count == 0
    err = capsys.readouterr().err
    assert "These GPU models qualified before that filter: A40, RTX 4090." in err
