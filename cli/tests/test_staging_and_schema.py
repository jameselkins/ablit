from __future__ import annotations

import copy

import pytest

from ablit.catalog import check_entry, schema_check
from ablit.catalog import schema_path
from ablit.catalog.models import Entry
from ablit.catalog.staging import StagingError, check_claim, compute_staging
from ablit.catalog.sources import profile_numbers, uncovered_numbers

REV = "a" * 40
SHA = "b" * 64
BLOB = "c" * 40


def base_entry() -> dict:
    return {
        "schema": 1,
        "id": "test-entry",
        "display_name": "Test entry",
        "source": {"repo": "owner/name", "revision": REV, "license": "mit", "modification": "none"},
        "artifacts": [
            {"path": "part-1", "bytes": 100, "sha256": SHA, "role": "weight"},
            {"path": "part-2", "bytes": 100, "sha256": SHA, "role": "weight"},
            {"path": "tok.json", "bytes": 10, "git_blob_id": BLOB, "role": "tokenizer"},
            {"repo": "other/base", "revision": REV, "path": "shard-1", "bytes": 300, "sha256": SHA, "role": "engram-source"},
        ],
        "staging": [
            {"step": "download", "artifacts": "all"},
            {"step": "join", "inputs": ["weight"], "output_bytes": 200, "delete_inputs_after": True},
            {"step": "engram-extract", "inputs": ["engram-source"], "output_bytes": 250,
             "keeps_inputs_until_done": True, "delete_inputs_after": True},
        ],
        "profile": {
            "engine": "xolotl", "engine_lock": "xolotl-961d390",
            "gpu": {"vendor": "nvidia", "count": 1, "vram_gb_each": 24, "compute_capability_min": 7.5},
            "host_ram_gb": 90, "storage": {"random_read_required": True},
            "context_tokens": 4096, "concurrency": 1, "port": 8150,
            "startup_hours_estimate": 0.5, "min_download_mbps": 500,
        },
        "sources": [{"url": "https://example.org/readme", "covers": ["profile.*"]}],
        "tests": [],
        "status": "unverified",
    }


def test_base_entry_is_valid():
    assert check_entry(base_entry()) == []


@pytest.mark.parametrize("source", [{}, {"repo": "other/model"}, {"revision": "d" * 40}])
@pytest.mark.parametrize("path", ["part-1", "./part-1", ".//part-1"])
@pytest.mark.parametrize("unfilled", [False, True])
def test_artifacts_must_have_distinct_download_destinations(source, path, unfilled):
    data = base_entry()
    duplicate = {**data["artifacts"][0], **source, "path": path}
    if unfilled:
        duplicate["bytes"] = None
    data["artifacts"].append(duplicate)
    problems = check_entry(data)
    assert any("download destination" in problem and path in problem for problem in problems)


def test_different_directories_can_hold_artifacts_with_the_same_basename():
    data = base_entry()
    data["artifacts"].append({**data["artifacts"][0], "path": "draft/part-1", "repo": "other/model"})
    assert check_entry(data) == []


@pytest.mark.parametrize("parent,child", [
    ("part-1", "part-1/config.json"),
    ("nested/weights", "nested/./weights//config.json"),
])
@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("unfilled", [False, True])
def test_an_artifact_file_cannot_also_be_a_download_directory(parent, child, reverse, unfilled):
    data = base_entry()
    data["artifacts"][0]["path"] = parent
    data["artifacts"].append({**data["artifacts"][0], "path": child, "repo": "other/model"})
    if unfilled:
        data["artifacts"][0]["bytes"] = None
    if reverse:
        data["artifacts"].reverse()
    assert any("also an artifact file" in problem and parent in problem for problem in check_entry(data))


def test_a_shared_filename_prefix_does_not_make_a_directory_conflict():
    data = base_entry()
    data["artifacts"].append({**data["artifacts"][0], "path": "part-10/config.json"})
    assert check_entry(data) == []


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf")], ids=["nan", "infinity", "negative-infinity"])
@pytest.mark.parametrize("field", [
    "profile.host_ram_gb", "profile.min_download_mbps", "profile.startup_hours_estimate",
    "profile.gpu.vram_gb_each", "profile.gpu.compute_capability_min", "profile.storage.disk_gb_claimed",
])
def test_catalog_numbers_must_be_finite(field, value):
    data = base_entry()
    target = data
    *parents, key = field.split(".")
    for part in parents:
        target = target[part]
    target[key] = value
    assert any(field in problem and "finite number" in problem for problem in check_entry(data))


def test_staging_peak_is_computed_from_the_steps():
    report = compute_staging(Entry.from_dict(base_entry()))
    assert report.download_bytes == 510
    # download 510; join peaks at 710 and ends at 510 once the parts are deleted;
    # extraction peaks at 760 and ends at 460 once the source shard is deleted.
    assert report.peak_bytes == 760
    assert report.final_bytes == 460


def test_streaming_extract_lowers_the_peak():
    data = base_entry()
    data["staging"][2]["keeps_inputs_until_done"] = False
    report = compute_staging(Entry.from_dict(data))
    assert report.peak_bytes == 710


def test_nothing_deleted_gives_the_worst_case():
    data = base_entry()
    for step in data["staging"][1:]:
        step["delete_inputs_after"] = False
    assert compute_staging(Entry.from_dict(data)).peak_bytes == 510 + 200 + 250


def test_unknown_sizes_are_an_error_not_a_zero():
    data = base_entry()
    data["artifacts"][0]["bytes"] = None
    with pytest.raises(StagingError):
        compute_staging(Entry.from_dict(data))


def test_staging_input_must_name_a_path_or_role():
    data = base_entry()
    data["staging"][1]["inputs"] = ["nothing"]
    problems = check_entry(data)
    assert any("matches no artifact" in p for p in problems)


@pytest.mark.parametrize("step_name", ["join", "engram-extract"])
@pytest.mark.parametrize("missing", ["never-downloaded", "partly-downloaded", "already-deleted"])
def test_transform_inputs_must_be_present_when_the_step_runs(step_name, missing):
    data = base_entry()
    transform = {"step": step_name, "inputs": ["weight"], "output_bytes": 200,
                 "keeps_inputs_until_done": True, "delete_inputs_after": True}
    if missing == "never-downloaded":
        data["staging"] = [transform, {"step": "download", "artifacts": "all"}]
    elif missing == "partly-downloaded":
        data["staging"] = [{"step": "download", "artifacts": ["part-1"]}, transform]
    else:
        data["staging"] = [{"step": "download", "artifacts": "all"}, transform, transform]
    with pytest.raises(StagingError, match="inputs are not present"):
        compute_staging(Entry.from_dict(data))
    assert any("inputs are not present" in problem for problem in check_entry(data))


@pytest.mark.parametrize("redownload", [False, True])
def test_a_later_transform_can_reuse_retained_or_downloaded_again_inputs(redownload):
    data = base_entry()
    first_join = data["staging"][1]
    first_join["delete_inputs_after"] = redownload
    repeat = copy.deepcopy(first_join)
    repeated_steps = ([{"step": "download", "artifacts": ["weight"]}] if redownload else []) + [repeat]
    data["staging"][2:2] = repeated_steps
    report = compute_staging(Entry.from_dict(data))
    assert report.download_bytes == (710 if redownload else 510)
    assert report.final_bytes == (660 if redownload else 860)
    assert check_entry(data) == []


def test_claim_below_the_computed_peak_is_reported():
    data = base_entry()
    data["profile"]["storage"]["disk_gb_claimed"] = 0.0000005
    problems = check_entry(data)
    assert any("above the claimed" in p for p in problems)
    data["profile"]["storage"]["disk_gb_claimed"] = 1.0
    assert check_entry(data) == []
    report = compute_staging(Entry.from_dict(data))
    assert check_claim(Entry.from_dict(data), report) == []


def test_every_profile_number_needs_a_source():
    data = base_entry()
    data["sources"] = [{"url": "https://example.org/x", "covers": ["profile.gpu.*", "profile.port"]}]
    missing = uncovered_numbers(data)
    assert "profile.host_ram_gb" in missing and "profile.gpu.count" not in missing
    problems = check_entry(data)
    assert any("profile.host_ram_gb has no source" in p for p in problems)


def test_profile_numbers_ignore_booleans_and_nulls():
    numbers = profile_numbers({"a": 1, "b": True, "c": None, "d": {"e": 2.5, "f": "x"}})
    assert numbers == ["profile.a", "profile.d.e"]


def test_a_bare_url_cites_the_entry_but_covers_no_number():
    data = base_entry()
    data["sources"] = ["https://example.org/x"]
    assert uncovered_numbers(data)


@pytest.mark.parametrize("mutate,fragment", [
    (lambda d: d.update(extra=1), "unknown field"),
    (lambda d: d.update(schema=2), "must be 1"),
    (lambda d: d["source"].update(revision="main"), "does not match"),
    (lambda d: d["source"].update(modification="tuned"), "is not one of"),
    (lambda d: d["artifacts"][0].update(role="mystery"), "is not one of"),
    (lambda d: d["artifacts"][0].update(sha256="xyz"), "does not match"),
    (lambda d: d["artifacts"][0].update(path="/abs/path"), "does not match"),
    (lambda d: d["artifacts"][0].update(path="../escape"), "does not match"),
    (lambda d: d["artifacts"][0].update(bytes=-1), "below the minimum"),
    (lambda d: d["artifacts"][0].update(bytes="12"), "expected integer or null"),
    (lambda d: d["artifacts"][0].pop("bytes"), "missing required field 'bytes'"),
    (lambda d: d["profile"].update(engine="tgi"), "is not one of"),
    (lambda d: d["profile"]["gpu"].update(count=0), "below the minimum"),
    (lambda d: d["profile"].update(port=70000), "above the maximum"),
    (lambda d: d["profile"].pop("host_ram_gb"), "missing required field 'host_ram_gb'"),
    (lambda d: d.update(status="fine"), "is not one of"),
    (lambda d: d.update(status="boot-tested"), "needs at least 1 items"),
    (lambda d: d["sources"].__setitem__(0, "http://insecure.example"), "matches none"),
    (lambda d: d["staging"].__setitem__(0, {"step": "run", "command": "rm -rf /"}), "step"),
    (lambda d: d.update(id="Bad ID"), "does not match"),
])
def test_schema_rejects_bad_entries(mutate, fragment):
    data = copy.deepcopy(base_entry())
    mutate(data)
    problems = check_entry(data)
    assert problems, "the entry should have been rejected"
    assert any(fragment in p for p in problems), problems


def test_null_byte_counts_are_allowed_until_filled():
    data = base_entry()
    data["artifacts"][0]["bytes"] = None
    data["artifacts"][0]["sha256"] = None
    data["staging"][1]["output_bytes"] = None
    assert check_entry(data) == []


def test_schema_file_is_valid_json_schema_shape(repo_root):
    schema = schema_check.load_schema(schema_path())
    assert schema["$schema"].startswith("https://json-schema.org/")
    assert schema["properties"]["schema"] == {"const": 1}
    assert schema["additionalProperties"] is False


def test_checker_handles_the_keywords_it_claims_to_support():
    schema = {
        "type": "object", "required": ["a"], "additionalProperties": False,
        "properties": {
            "a": {"type": ["integer", "null"], "minimum": 1},
            "b": {"anyOf": [{"const": "all"}, {"type": "array", "minItems": 1, "items": {"type": "string"}}]},
            "c": {"oneOf": [{"type": "string"}, {"type": "integer"}]},
        },
    }
    assert schema_check.validate({"a": None}, schema) == []
    assert schema_check.validate({"a": 0}, schema)
    assert schema_check.validate({"a": 1, "b": "all", "c": 3}, schema) == []
    assert schema_check.validate({"a": 1, "b": []}, schema)
    assert schema_check.validate({"a": 1, "b": [1]}, schema)
    assert schema_check.validate({"a": True}, schema)  # a boolean is not an integer
    assert schema_check.validate({"a": 1, "z": 1}, schema)
