"""Checks on the seed catalog in catalog/models/."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from ablit.catalog import check_entry, load_catalog, load_entry_file, resolve
from ablit.catalog.models import Entry
from ablit.catalog.staging import compute_staging

SEED_IDS = {
    "ds41-1017-abl-fp8",
    "ds41-september-gsq-uncensored",
    "ds41-1017-xq-v3-uncensored",
    "llama33-70b-fp8-dynamic",
    "llama31-8b-abliterated-q4km",
}


def _entry_params():
    root = Path(__file__).resolve().parents[2] / "catalog" / "models"
    return sorted(root.glob("*.yaml"))


@pytest.mark.parametrize("path", _entry_params(), ids=lambda p: p.stem)
def test_entry_passes_verify_entry_offline(path: Path, repo_root: Path):
    result = subprocess.run(
        [sys.executable, str(repo_root / "tools" / "verify_entry.py"), "--offline", str(path)],
        capture_output=True, text=True, cwd=repo_root,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"ok   {path.name}" in result.stdout


def test_verify_entry_offline_with_no_arguments_covers_every_entry(repo_root: Path, entry_files):
    result = subprocess.run(
        [sys.executable, str(repo_root / "tools" / "verify_entry.py"), "--offline"],
        capture_output=True, text=True, cwd=repo_root,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    for path in entry_files:
        assert f"ok   {path.name}" in result.stdout


def test_verify_entry_refuses_fill_without_the_network(repo_root: Path):
    result = subprocess.run(
        [sys.executable, str(repo_root / "tools" / "verify_entry.py"), "--offline", "--fill"],
        capture_output=True, text=True, cwd=repo_root,
    )
    assert result.returncode != 0


def test_the_seven_seed_entries_exist(entry_files):
    assert {p.stem for p in entry_files} == SEED_IDS


@pytest.mark.parametrize("path", _entry_params(), ids=lambda p: p.stem)
def test_seed_entry_facts(path: Path):
    data = load_entry_file(path)
    assert data["id"] == path.stem
    assert data["status"] == "unverified"
    assert data["tests"] == []
    assert data["schema"] == 1
    assert check_entry(data) == []


@pytest.mark.parametrize("path", _entry_params(), ids=lambda p: p.stem)
def test_artifacts_are_filled_in(path: Path):
    entry = Entry.from_dict(load_entry_file(path))
    for artifact in entry.artifacts:
        assert artifact.bytes is not None and artifact.bytes > 0, artifact.path
        assert artifact.sha256 or artifact.git_blob_id, artifact.path
    for step in entry.staging:
        if step.step != "download":
            assert step.output_bytes
    report = compute_staging(entry)
    assert report.peak_bytes >= report.download_bytes > 0


@pytest.mark.parametrize("path", _entry_params(), ids=lambda p: p.stem)
def test_sources_are_primary_public_urls(path: Path):
    text = path.read_text(encoding="utf-8")
    for needle in ("/Users/", "file://", "ablit-planning", "/private/", "localhost"):
        assert needle not in text, (path.name, needle)
    data = yaml.safe_load(text)
    urls = [s if isinstance(s, str) else s["url"] for s in data["sources"]]
    assert urls and all(u.startswith("https://") for u in urls)
    allowed = ("huggingface.co", "github.com")
    assert all(u.split("/")[2] in allowed for u in urls), urls


@pytest.mark.parametrize("path", _entry_params(), ids=lambda p: p.stem)
def test_every_floor_is_labeled_as_not_yet_tested(path: Path):
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert any("not yet tested" in note for note in data["notes"])
    assert "unverified" in path.read_text(encoding="utf-8").splitlines()[0]


def test_the_1017_xq_entry_holds_only_the_v3_build():
    for variant, other in (("v3", "v2"),):
        data = load_entry_file(Path(__file__).resolve().parents[2] / "catalog" / "models" / f"ds41-1017-xq-{variant}-uncensored.yaml")
        paths = [a["path"] for a in data["artifacts"]]
        assert any(p.startswith(f"{variant}/") for p in paths)
        assert not any(p.startswith(f"{other}/") for p in paths)
        assert "V41-MTP-Vision-1017.Q2_K.gguf" in paths
        assert not any(p == "V41-MTP-Vision-1017.gguf" for p in paths)
        assert {p for p in paths if p.startswith("tokenizer/")} == {
            "tokenizer/chat_template.jinja", "tokenizer/merges.txt", "tokenizer/token_type.json", "tokenizer/vocab.json"}
        shards = [a for a in data["artifacts"] if a["role"] == "engram-source"]
        assert [a["path"] for a in shards] == ["model-00047-of-00048.safetensors", "model-00048-of-00048.safetensors"]
        assert all(a["repo"] == "deepseek-ai/DeepSeek-V4.1-Flash-1017" and
                   a["revision"] == "1bae0e1088993b9b82de08f59de5be5a34e42170" for a in shards)
        assert data["source"]["revision"] == "9d3c01d86709261e51d566b02e1dda75f40d009a"


def test_load_catalog_and_resolve_by_id_and_repo(repo_root: Path):
    result = load_catalog(repo_root / "catalog" / "models")
    assert not result.problems
    assert {e.id for e in result.entries} == SEED_IDS
    assert resolve(result.entries, "llama31-8b-abliterated-q4km").profile.engine == "llamacpp"
    single = resolve(result.entries, "mlabonne/Meta-Llama-3.1-8B-Instruct-abliterated-GGUF")
    assert single.id == "llama31-8b-abliterated-q4km"
    from ablit.catalog import AmbiguousModel, ModelNotInCatalog

    # The curated catalog has one build per repo, so a second build is added here.
    twin = result.entries[0].model_copy(update={"id": "twin-build"})
    entries = [*result.entries, twin]
    repo = twin.source.repo
    with pytest.raises(AmbiguousModel) as info:
        resolve(entries, repo)
    assert set(info.value.candidates) == {result.entries[0].id, "twin-build"}
    assert resolve(entries, repo, variant="twin-build").id == "twin-build"
    with pytest.raises(ModelNotInCatalog):
        resolve(result.entries, "someone/not-in-the-catalog")


def test_planner_request_comes_from_the_entry(repo_root: Path):
    from ablit.select.request import request_from_entry

    entries = {e.id: e for e in load_catalog(repo_root / "catalog" / "models").entries}
    req = request_from_entry(entries["ds41-1017-abl-fp8"])
    assert req.gpu_count == 8 and req.vram_gb_total == 614
    assert req.download_gb == pytest.approx(510.3, abs=0.1)
    xq = request_from_entry(entries["ds41-1017-xq-v3-uncensored"])
    assert xq.gpu_vendor == "nvidia" and xq.gpu_count == 1 and xq.vram_gb_each == 24
    assert xq.disk_gb > xq.download_gb


def test_a_broken_entry_is_reported_and_the_rest_still_load(tmp_path: Path, repo_root: Path):
    for path in (repo_root / "catalog" / "models").glob("*.yaml"):
        (tmp_path / path.name).write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    (tmp_path / "broken.yaml").write_text("schema: 1\nid: broken\n", encoding="utf-8")
    result = load_catalog(tmp_path)
    assert "broken.yaml" in result.problems
    assert len(result.entries) == len(SEED_IDS)
