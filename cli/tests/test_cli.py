from __future__ import annotations

import sys
from pathlib import Path

import pytest

from ablit import exitcodes
from ablit.cli import build_parser, main
from ablit.notices import SPEND_CAP_NOTICE, SPOT_WARNING


def run(capsys, *argv: str):
    code = main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_catalog_list_shows_every_entry_and_its_status(capsys):
    code, out, err = run(capsys, "catalog", "list")
    assert code == 0, err
    for entry_id in ("ds41-1017-abl-fp8", "ds41-1017-xq-v3-uncensored", "llama31-8b-abliterated-q4km"):
        assert entry_id in out
    assert out.count("unverified") == 7


def test_catalog_show_explains_one_entry(capsys):
    code, out, err = run(capsys, "catalog", "show", "ds41-1017-xq-v3-uncensored")
    assert code == 0, err
    assert "not yet tested" in out
    assert "Nobody has booted this entry yet" in out
    assert "Peak disk:" in out and "GB" in out
    assert "https://github.com/Indras-Mirror/Xolotl" in out


def test_catalog_show_unknown_model_exits_3_with_a_link(capsys):
    code, out, err = run(capsys, "catalog", "show", "someone/unknown-model")
    assert code == exitcodes.NOT_SUPPORTED
    assert "CONTRIBUTING.md" in err


def test_catalog_show_ambiguous_repo_asks_for_a_variant(capsys):
    code, out, err = run(capsys, "catalog", "show", "IndrasMirror/DeepSeek-V4.1-Flash-1017-XQ-Uncensored")
    assert code == exitcodes.USAGE
    assert "--variant" in err and "ds41-1017-xq-v2-uncensored" in err
    code, out, err = run(capsys, "catalog", "show", "IndrasMirror/DeepSeek-V4.1-Flash-1017-XQ-Uncensored",
                         "--variant", "ds41-1017-xq-v2-uncensored")
    assert code == 0, err
    assert "ds41-1017-xq-v2-uncensored" in out


def test_catalog_validate_accepts_a_seed_file_and_rejects_a_bad_one(capsys, tmp_path: Path, repo_root: Path):
    seed = repo_root / "catalog" / "models" / "llama31-8b-abliterated-q4km.yaml"
    code, out, err = run(capsys, "catalog", "validate", str(seed))
    assert code == 0, err
    assert "ok" in out

    bad = tmp_path / "bad.yaml"
    bad.write_text("schema: 1\nid: Bad Id\n", encoding="utf-8")
    code, out, err = run(capsys, "catalog", "validate", str(bad))
    assert code == exitcodes.INVALID_FILE
    assert "missing required field" in err

    missing = tmp_path / "missing.yaml"
    code, out, err = run(capsys, "catalog", "validate", str(missing))
    assert code == exitcodes.INVALID_FILE


@pytest.mark.parametrize("argv", [
    ["setup"], ["setup", "vast"], ["setup", "--no-referral"],
    ["status"], ["status", "run1", "--json", "--watch", "--show-key"], ["status", "--all"],
    ["logs"], ["logs", "run1", "--stage", "bootstrap", "-f"],
    ["tunnel"], ["tunnel", "run1"], ["down"], ["down", "run1"],
])
def test_commands_without_code_exit_2_and_say_so(capsys, argv):
    code, out, err = run(capsys, *argv)
    assert code == 2
    assert "not implemented yet" in err


def test_plan_resolves_the_model_then_says_it_is_not_implemented(capsys):
    code, out, err = run(capsys, "plan", "llama31-8b-abliterated-q4km", "--hours", "2", "--spot", "--trust", "verified",
                         "--min-download", "300", "--min-reliability", "0.9", "--region", "us", "--max-price-hr", "4",
                         "--backends", "vast,runpod", "--endpoint", "public", "--json")
    assert code == 2
    assert "not implemented yet" in err


def test_plan_for_a_model_outside_the_catalog_exits_3(capsys):
    code, out, err = run(capsys, "plan", "someone/unknown-model")
    assert code == exitcodes.NOT_SUPPORTED
    assert "CONTRIBUTING.md" in err


def test_up_needs_max_spend_and_has_no_default(capsys):
    code, out, err = run(capsys, "up", "llama31-8b-abliterated-q4km")
    assert code == 2
    assert "--max-spend is required" in err


def test_up_without_a_terminal_exits_2(capsys, monkeypatch):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False, raising=False)
    code, out, err = run(capsys, "up", "llama31-8b-abliterated-q4km", "--max-spend", "5")
    assert code == 2
    assert "needs a terminal" in err


def test_up_help_prints_the_spend_cap_notice(capsys):
    with pytest.raises(SystemExit) as info:
        main(["up", "--help"])
    assert info.value.code == 0
    out = capsys.readouterr().out
    assert " ".join(SPEND_CAP_NOTICE.split()) in " ".join(out.split())
    assert " ".join(SPOT_WARNING.split()) in " ".join(out.split())


def test_there_is_no_yes_flag():
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["up", "m", "--yes"])


def test_every_documented_flag_parses():
    parser = build_parser()
    args = parser.parse_args([
        "up", "model", "--max-spend", "12.5", "--hours", "3", "--spot", "--min-download", "250",
        "--trust", "verified", "--min-reliability", "0.95", "--region", "us", "--max-price-hr", "5",
        "--backends", "vast", "--variant", "v", "--endpoint", "public", "--untested", "--catalog", "x",
        "--json", "--another", "--report"])
    assert args.max_spend == 12.5 and args.hours == 3 and args.spot and args.untested and args.another and args.report
    args = parser.parse_args(["plan", "model"])
    assert args.hours == 1.0 and args.trust == "any" and args.endpoint == "tunnel" and not args.spot
    args = parser.parse_args(["logs", "r", "--stage", "watchdog", "-f"])
    assert args.stage == "watchdog" and args.follow
    with pytest.raises(SystemExit):
        parser.parse_args(["logs", "r", "--stage", "other"])


def test_no_command_prints_help_and_exits_2(capsys):
    code, out, err = run(capsys)
    assert code == 2
    assert "usage" in err
