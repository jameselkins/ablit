"""Keep boot-test claims attached to the build that actually ran."""

from __future__ import annotations

import copy
import hashlib
import io
import shutil
import tarfile
import pytest
import yaml

from ablit.catalog import check_entry, load_entry_file
from ablit.catalog import loader
from ablit.catalog.schema_check import load_schema
from ablit.catalog.models import Entry
from ablit.runs.monitor import boot_report, check_bootstrap


@pytest.fixture
def tested_entry(repo_root):
    return load_entry_file(repo_root / "catalog/models/llama31-8b-abliterated-q4km.yaml")


def test_a_boot_test_without_build_provenance_cannot_approve_a_launch(tested_entry):
    tested_entry["tests"] = [{"backend": "vast", "date": "2026-10-21", "log": "boot-tests/example.md"}]
    assert any("matching boot test" in p for p in check_entry(tested_entry))


@pytest.mark.parametrize("mutate", [
    lambda d: d["profile"].update(context_tokens=8192),
    lambda d: d["profile"].update(concurrency=2),
    lambda d: d["source"].update(revision="b" * 40),
    lambda d: d["artifacts"][0].update(sha256="c" * 64),
])
def test_a_changed_build_cannot_keep_its_tested_status(tested_entry, mutate):
    mutate(tested_entry)
    assert any("matching boot test" in p for p in check_entry(tested_entry))


def test_notes_and_display_names_do_not_invalidate_a_boot_test(tested_entry):
    tested_entry["display_name"] = "A clearer name"
    tested_entry["notes"].append("Extra explanation of the tested hardware.")
    assert check_entry(tested_entry) == []


def test_a_changed_image_cannot_keep_its_tested_status(tested_entry, repo_root, tmp_path, monkeypatch):
    schema = load_schema(repo_root / "catalog/schema/v1.json")
    image_path = tmp_path / "engines/llamacpp/lock.yaml"
    image_path.parent.mkdir(parents=True)
    image = yaml.safe_load((repo_root / "engines/llamacpp/lock.yaml").read_text())
    image["digest"] = "sha256:" + "d" * 64
    image_path.write_text(yaml.safe_dump(image))
    shutil.copy(repo_root / "engines/llamacpp/bootstrap.sh", image_path.with_name("bootstrap.sh"))
    monkeypatch.setattr(loader, "repo_root", lambda: tmp_path)
    assert any("matching boot test" in p for p in check_entry(tested_entry, schema))


def test_unverified_entries_can_keep_historical_test_records(tested_entry):
    tested_entry["status"] = "unverified"
    tested_entry["profile"]["context_tokens"] = 8192
    assert check_entry(tested_entry) == []


def test_changed_bootstrap_defaults_cannot_keep_tested_status(tested_entry, repo_root, tmp_path, monkeypatch):
    schema = load_schema(repo_root / "catalog/schema/v1.json")
    runtime = tmp_path / "engines/llamacpp"
    runtime.mkdir(parents=True)
    shutil.copy(repo_root / "engines/llamacpp/lock.yaml", runtime / "lock.yaml")
    bootstrap = (repo_root / "engines/llamacpp/bootstrap.sh").read_text()
    (runtime / "bootstrap.sh").write_text(bootstrap + '\nexport LLAMA_ARG_N_PARALLEL=2\n')
    monkeypatch.setattr(loader, "repo_root", lambda: tmp_path)
    assert any("matching boot test" in p for p in check_entry(tested_entry, schema))


def test_report_from_an_older_run_preserves_its_image_and_marks_bootstrap_unknown(tested_entry):
    old_image = "example.invalid/engine@sha256:" + "a" * 64
    record = {"model_id": tested_entry["id"], "entry": Entry.from_dict(tested_entry).model_dump(),
              "image": old_image, "cap_usd": 2, "spend_estimate_usd": 0.25}
    report = boot_report(record)
    assert report["image"] == old_image
    assert report["image_digest"] == "sha256:" + "a" * 64
    assert report["bootstrap_sha256"] is None
    assert len(report["build_sha256"]) == 64


def test_report_keeps_the_provenance_recorded_at_launch(tested_entry):
    evidence = tested_entry["tests"][0]
    record = {"model_id": tested_entry["id"], "image": "example.invalid/engine@" + evidence["image_digest"],
              "image_digest": evidence["image_digest"], "build_sha256": evidence["build_sha256"],
              "bootstrap_sha256": evidence["bootstrap_sha256"], "cap_usd": 2, "spend_estimate_usd": 0.25}
    report = boot_report(record)
    for field in ("image_digest", "build_sha256", "bootstrap_sha256"):
        assert report[field] == record[field]


def _bundle(script):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as archive:
        info = tarfile.TarInfo("engine/bootstrap.sh")
        info.size = len(script)
        archive.addfile(info, io.BytesIO(script))
    return output.getvalue()


def test_resume_refuses_a_changed_bootstrap_instead_of_overwriting_its_provenance():
    recorded = hashlib.sha256(b"original script").hexdigest()
    record = {"bootstrap_sha256": recorded}
    with pytest.raises(ValueError, match="bootstrap changed"):
        check_bootstrap(record, _bundle(b"changed script"))
    assert record["bootstrap_sha256"] == recorded


def test_resume_keeps_a_matching_bootstrap_and_does_not_invent_legacy_provenance():
    script = b"original script"
    record = {"bootstrap_sha256": hashlib.sha256(script).hexdigest()}
    check_bootstrap(record, _bundle(script))
    legacy = {}
    check_bootstrap(legacy, _bundle(script))
    assert legacy == {}


def test_a_matching_test_can_follow_an_older_record(tested_entry):
    old = copy.deepcopy(tested_entry["tests"][0])
    old["image_digest"] = "sha256:" + "e" * 64
    tested_entry["tests"].insert(0, old)
    assert check_entry(tested_entry) == []
