from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

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
    assert out.count("unverified") == 4
    assert out.count("boot-tested") == 5


def test_catalog_show_explains_one_entry(capsys):
    code, out, err = run(capsys, "catalog", "show", "ds41-1017-xq-v3-uncensored")
    assert code == 0, err
    assert "not yet tested" in out
    assert "Tests:        vast on 2026-10-22" in out
    assert "Peak disk:" in out and "GB" in out
    assert "https://github.com/Indras-Mirror/Xolotl" in out
    code, out, err = run(capsys, "catalog", "show", "llama33-70b-fp8-dynamic")
    assert code == 0, err
    assert "Nobody has booted this entry yet" in out


def test_catalog_show_unknown_model_exits_3_with_a_link(capsys):
    code, out, err = run(capsys, "catalog", "show", "someone/unknown-model")
    assert code == exitcodes.NOT_SUPPORTED
    assert "CONTRIBUTING.md" in err


@pytest.mark.parametrize("field", ["display_name", "notes"])
def test_catalog_show_removes_terminal_controls_from_entry_text(capsys, tmp_path, repo_root, field):
    model = "llama31-8b-abliterated-q4km"
    seed = repo_root / "catalog/models" / (model + ".yaml")
    data = yaml.safe_load(seed.read_text(encoding="utf-8"))
    text = "Invented modèle\x1b[2J\x1b]52;c;aW52ZW50ZWQ=\x07\x9b31m"
    data[field] = [text] if field == "notes" else text
    (tmp_path / seed.name).write_text(yaml.safe_dump(data), encoding="utf-8")
    code, out, err = run(capsys, "catalog", "show", model, "--catalog", str(tmp_path))
    assert code == 0 and err == ""
    assert "Invented modèle" in out
    assert not any(control in out for control in ("\x1b", "\x07", "\x9b"))
    assert "Source repo:" in out and "Sources:\n" in out


@pytest.mark.parametrize(("model", "allowance", "displayed"), [
    ("llama31-8b-abliterated-q4km", 10.0, "15.0"),
    ("ds41-1017-xq-v3-uncensored", 232.0, "603.8"),
])
def test_catalog_show_includes_the_full_planner_disk_floor(capsys, model, allowance, displayed):
    from ablit.catalog import load_catalog, resolve
    from ablit.catalog.staging import compute_staging
    from ablit.select.request import request_from_entry

    entry = resolve(load_catalog().entries, model)
    minimum = compute_staging(entry).peak_gb + allowance
    assert request_from_entry(entry).disk_gb == pytest.approx(minimum)
    code, out, err = run(capsys, "catalog", "show", model)
    assert code == 0, err
    assert minimum <= float(displayed) < minimum + 0.1
    assert f"Required disk: at least {displayed} GB (including the engine image and temporary disk allowances)" in out


@pytest.mark.parametrize(("model", "cuda", "generations"), [
    ("ds41-1017-xq-v3-uncensored", "13.0", "8.6, 8.9, 12.0"),
    ("llama31-8b-abliterated-q4km", "12.8", None),
])
@pytest.mark.parametrize("unfilled", [False, True])
def test_catalog_show_includes_engine_gpu_and_driver_requirements(capsys, tmp_path, repo_root, model, cuda, generations, unfilled):
    seed = repo_root / "catalog/models" / (model + ".yaml")
    data = yaml.safe_load(seed.read_text(encoding="utf-8"))
    if unfilled:
        data["artifacts"][0]["bytes"] = None
    (tmp_path / seed.name).write_text(yaml.safe_dump(data), encoding="utf-8")
    code, out, err = run(capsys, "catalog", "show", model, "--catalog", str(tmp_path))
    assert code == 0, err
    assert f"Host CUDA:    driver must support CUDA {cuda} or newer" in out
    if generations:
        assert f"Engine GPU generations: {generations} only" in out
    else:
        assert "Engine GPU generations:" not in out
    if unfilled:
        assert "Peak disk:    unknown" in out
        assert "Required disk: unknown" in out


def test_catalog_show_keeps_an_entry_inspectable_when_its_engine_lock_does_not_match(capsys, tmp_path, repo_root):
    model = "llama31-8b-abliterated-q4km"
    seed = repo_root / "catalog/models" / (model + ".yaml")
    data = yaml.safe_load(seed.read_text(encoding="utf-8"))
    data["profile"]["engine_lock"] = "invented-lock"
    (tmp_path / seed.name).write_text(yaml.safe_dump(data), encoding="utf-8")
    code, out, err = run(capsys, "catalog", "show", model, "--catalog", str(tmp_path))
    assert code == 0, err
    assert "invented-lock" in out
    assert "Host CUDA:" not in out and "Engine GPU generations:" not in out
    assert "Required disk: unknown" in out


@pytest.mark.parametrize("problem", ["mismatch", "missing"])
@pytest.mark.parametrize("json_output", [False, True])
def test_plan_requires_the_entrys_engine_lock_before_searching(capsys, tmp_path, repo_root, monkeypatch, problem, json_output):
    model = "llama31-8b-abliterated-q4km"
    seed = repo_root / "catalog/models" / (model + ".yaml")
    data = yaml.safe_load(seed.read_text(encoding="utf-8"))
    if problem == "mismatch":
        data["profile"]["engine_lock"] = "invented-lock"
    else:
        monkeypatch.setattr("ablit.engines.repo_root", lambda: tmp_path)
    (tmp_path / seed.name).write_text(yaml.safe_dump(data), encoding="utf-8")

    class NoSearch:
        def search(self, *args):
            pytest.fail("an unusable engine lock must stop before marketplace searches")

    flags = ["--json"] if json_output else []
    assert main(["plan", model, "--catalog", str(tmp_path), *flags], backends={"vast": NoSearch()}, environ={}) == 2
    captured = capsys.readouterr()
    assert captured.out == "" and "cannot be planned yet" in captured.err
    assert "engine lock" in captured.err.lower() or "image lock" in captured.err.lower()


@pytest.mark.parametrize("text", ["", "[]", "a scalar", "{}", "name: [",
                                  "name: llamacpp-server-cuda\nimage_gb: .nan\n"])
@pytest.mark.parametrize("command", ["plan", "show"])
def test_unreadable_engine_lock_shapes_do_not_break_catalog_commands(capsys, tmp_path, monkeypatch, text, command):
    model = "llama31-8b-abliterated-q4km"
    directory = tmp_path / "engines/llamacpp"
    directory.mkdir(parents=True)
    (directory / "lock.yaml").write_text(text, encoding="utf-8")
    monkeypatch.setattr("ablit.engines.repo_root", lambda: tmp_path)

    class NoSearch:
        def search(self, *args):
            pytest.fail("an invalid engine lock must stop before searches")

    argv = ["plan", model, "--json"] if command == "plan" else ["catalog", "show", model]
    code = main(argv, backends={"vast": NoSearch()}, environ={})
    captured = capsys.readouterr()
    if command == "plan":
        assert code == 2 and captured.out == ""
        assert "engine lock is unavailable" in captured.err
    else:
        assert code == 0 and captured.err == ""
        assert model in captured.out and "Required disk: unknown" in captured.out


def test_catalog_list_reports_a_filename_mismatch_and_keeps_valid_entries(capsys, tmp_path, repo_root):
    seed = repo_root / "catalog/models/llama31-8b-abliterated-q4km.yaml"
    text = seed.read_text(encoding="utf-8")
    (tmp_path / seed.name).write_text(text, encoding="utf-8")
    (tmp_path / "copied.yaml").write_text(text, encoding="utf-8")
    code, out, err = run(capsys, "catalog", "list", "--catalog", str(tmp_path))
    assert code == exitcodes.INVALID_FILE
    assert out.count("llama31-8b-abliterated-q4km") == 1
    assert "copied.yaml was skipped" in err and "file name must match" in err


def test_catalog_list_skips_invalid_utf8_without_hiding_valid_entries(capsys, tmp_path, repo_root):
    seed = repo_root / "catalog/models/llama31-8b-abliterated-q4km.yaml"
    (tmp_path / seed.name).write_bytes(seed.read_bytes())
    (tmp_path / "broken.yaml").write_bytes(b"display_name: caf\xe9\n")
    code, out, err = run(capsys, "catalog", "list", "--catalog", str(tmp_path))
    assert code == exitcodes.INVALID_FILE
    assert "llama31-8b-abliterated-q4km" in out
    assert "broken.yaml was skipped" in err and "UTF-8" in err


@pytest.mark.parametrize("value, reason", [
    ("2026-13-01", "month"),
    ("2026-02-30", "day"),
    ("2026-01-01T00:00:00+25:00", "offset"),
])
def test_catalog_commands_report_invalid_yaml_dates_and_keep_valid_entries(
    capsys, tmp_path, repo_root, value, reason
):
    seed = repo_root / "catalog/models/llama31-8b-abliterated-q4km.yaml"
    (tmp_path / seed.name).write_bytes(seed.read_bytes())
    broken = tmp_path / "broken.yaml"
    broken.write_text(f"notes: [{value}]\n", encoding="utf-8")

    code, out, err = run(capsys, "catalog", "list", "--catalog", str(tmp_path))
    assert code == exitcodes.INVALID_FILE
    assert "llama31-8b-abliterated-q4km" in out
    assert "broken.yaml was skipped" in err and reason in err

    code, out, err = run(capsys, "catalog", "validate", str(broken))
    assert code == exitcodes.INVALID_FILE and out == ""
    assert "broken.yaml" in err and reason in err


def test_catalog_commands_report_deep_yaml_without_hiding_valid_entries(capsys, tmp_path, repo_root):
    seed = repo_root / "catalog/models/llama31-8b-abliterated-q4km.yaml"
    (tmp_path / seed.name).write_bytes(seed.read_bytes())
    broken = tmp_path / "broken.yaml"
    depth = sys.getrecursionlimit() + 10
    broken.write_text("notes: " + "[" * depth + "text" + "]" * depth + "\n", encoding="utf-8")

    code, out, err = run(capsys, "catalog", "list", "--catalog", str(tmp_path))
    assert code == exitcodes.INVALID_FILE
    assert "llama31-8b-abliterated-q4km" in out
    assert "broken.yaml was skipped" in err and "nesting is too deep" in err

    code, out, err = run(capsys, "catalog", "validate", str(broken))
    assert code == exitcodes.INVALID_FILE and out == ""
    assert "broken.yaml" in err and "nesting is too deep" in err


def test_catalog_validate_reports_invalid_utf8_as_an_invalid_file(capsys, tmp_path):
    path = tmp_path / "broken.yaml"
    path.write_bytes(b"display_name: caf\xe9\n")
    code, out, err = run(capsys, "catalog", "validate", str(path))
    assert code == exitcodes.INVALID_FILE and out == ""
    assert "broken.yaml" in err and "UTF-8" in err


@pytest.mark.parametrize("command", ["plan", "up", "show"])
def test_model_commands_report_why_a_catalog_entry_was_skipped(capsys, tmp_path, repo_root, command):
    model = "llama31-8b-abliterated-q4km"
    seed = repo_root / "catalog/models" / (model + ".yaml")
    data = yaml.safe_load(seed.read_text(encoding="utf-8"))
    data["profile"]["gpu"]["count"] = 0
    (tmp_path / seed.name).write_text(yaml.safe_dump(data), encoding="utf-8")

    class NoSearch:
        def search(self, *args):
            pytest.fail("a rejected entry must stop before marketplace searches")

    argv = ["catalog", "show", model] if command == "show" else [command, model]
    code = main([*argv, "--catalog", str(tmp_path)], backends={"vast": NoSearch()}, environ={})
    captured = capsys.readouterr()
    assert code == exitcodes.NOT_SUPPORTED and captured.out == ""
    assert seed.name + " was skipped" in captured.err
    assert "$.profile.gpu.count: 0 is below the minimum 1" in captured.err


def test_plan_reports_skipped_entries_without_breaking_json_for_a_valid_model(capsys, tmp_path, repo_root):
    import json

    model = "llama31-8b-abliterated-q4km"
    seed = repo_root / "catalog/models" / (model + ".yaml")
    (tmp_path / seed.name).write_bytes(seed.read_bytes())
    (tmp_path / "broken.yaml").write_text("not a mapping\n", encoding="utf-8")
    code = main(["plan", model, "--catalog", str(tmp_path), "--json"], backends={}, environ={})
    captured = capsys.readouterr()
    assert code == exitcodes.NOT_SUPPORTED  # no marketplace offers were supplied
    report = json.loads(captured.out)
    assert report["model"]["id"] == model and report["complete"] is True
    assert "broken.yaml was skipped" in captured.err and "must hold a YAML mapping" in captured.err


def test_catalog_show_ambiguous_repo_asks_for_a_variant(capsys, tmp_path: Path, repo_root: Path):
    # The curated catalog has one build per repo, so a local catalog adds a second build.
    source = repo_root / "catalog" / "models" / "ds41-1017-xq-v3-uncensored.yaml"
    (tmp_path / source.name).write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    twin = source.read_text(encoding="utf-8").replace("id: ds41-1017-xq-v3-uncensored", "id: ds41-1017-xq-twin")
    (tmp_path / "ds41-1017-xq-twin.yaml").write_text(twin, encoding="utf-8")
    repo = "IndrasMirror/DeepSeek-V4.1-Flash-1017-XQ-Uncensored"
    code, out, err = run(capsys, "catalog", "show", repo, "--catalog", str(tmp_path))
    assert code == exitcodes.USAGE
    assert "--variant" in err and "ds41-1017-xq-twin" in err
    code, out, err = run(capsys, "catalog", "show", repo, "--catalog", str(tmp_path), "--variant", "ds41-1017-xq-twin")
    assert code == 0, err
    assert "ds41-1017-xq-twin" in out


@pytest.mark.parametrize("value", [".nan", ".inf", "-.inf"])
def test_catalog_validate_rejects_nonfinite_yaml_numbers(capsys, tmp_path, repo_root, value):
    seed = repo_root / "catalog/models/llama31-8b-abliterated-q4km.yaml"
    data = yaml.safe_load(seed.read_text(encoding="utf-8"))
    data["profile"]["host_ram_gb"] = yaml.safe_load(value)
    path = tmp_path / seed.name
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    code, out, err = run(capsys, "catalog", "validate", str(path))
    assert code == exitcodes.INVALID_FILE and not out
    assert "profile.host_ram_gb" in err and "finite number" in err


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
def test_run_commands_without_a_selected_run_or_terminal_exit_2(capsys, argv):
    code, out, err = run(capsys, *argv)
    if argv == ["status", "--all"]:
        assert code == 0 and "No runs" in out
        return
    assert code == 2
    assert "terminal" in err or "run" in err.lower()


def test_plan_refuses_spot_until_a_round_trip_has_been_tested(capsys):
    code, out, err = run(capsys, "plan", "llama31-8b-abliterated-q4km", "--hours", "2", "--spot", "--trust", "verified",
                         "--min-download", "300", "--min-reliability", "0.9", "--region", "us", "--max-price-hr", "4",
                         "--backends", "vast,runpod", "--endpoint", "public", "--json")
    assert code == 2
    assert "--spot is disabled" in err


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
    assert "--yes" in err


def test_up_help_prints_the_spend_cap_notice(capsys):
    with pytest.raises(SystemExit) as info:
        main(["up", "--help"])
    assert info.value.code == 0
    out = capsys.readouterr().out
    assert " ".join(SPEND_CAP_NOTICE.split()) in " ".join(out.split())
    assert " ".join(SPOT_WARNING.split()) in " ".join(out.split())


def test_yes_parses_only_on_up():
    parser = build_parser()
    assert parser.parse_args(["up", "m", "--yes"]).yes
    with pytest.raises(SystemExit):
        parser.parse_args(["plan", "m", "--yes"])


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
