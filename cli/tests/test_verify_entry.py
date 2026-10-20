"""Test the online half of tools/verify_entry.py with a fake Hugging Face client.

No test here touches the network.
"""

from __future__ import annotations

import copy
import hashlib
import urllib.error
from pathlib import Path

import pytest
import yaml

import verify_entry as ve

REV = "1" * 40
BASE_REV = "2" * 40


class FakeClient(ve.HFClient):
    """Serves canned API answers and file contents."""

    def __init__(self, repos: dict[tuple[str, str], dict], files: dict[tuple[str, str, str], bytes]):
        self.repos = repos
        self.files = files
        self.requests: list[str] = []

    def revision(self, repo, rev):
        self.requests.append(f"api:{repo}@{rev}")
        if (repo, rev) not in self.repos:
            raise urllib.error.HTTPError("u", 404, "not found", None, None)  # type: ignore[arg-type]
        return self.repos[(repo, rev)]

    def file(self, repo, rev, path):
        self.requests.append(f"file:{repo}@{rev}/{path}")
        return self.files[(repo, rev, path)]

    def head_bytes(self, repo, rev, path, count):
        return self.files[(repo, rev, path)][:count]


SMALL = b'{"hello": "world"}\n'
LFS_SHA = "9" * 64


def make_world():
    siblings = [
        {"rfilename": "weights.bin", "size": 134, "blobId": "f" * 40, "lfs": {"sha256": LFS_SHA, "size": 5000, "pointerSize": 134}},
        {"rfilename": "config.json", "size": len(SMALL), "blobId": ve.git_blob_id(SMALL)},
    ]
    repos = {
        ("owner/name", REV): {"sha": REV, "siblings": siblings, "cardData": {"license": "mit"}, "gated": False},
        ("other/base", BASE_REV): {
            "sha": BASE_REV, "gated": False,
            "siblings": [{"rfilename": "shard", "size": 100, "blobId": "e" * 40,
                          "lfs": {"sha256": "8" * 64, "size": 7000}}],
        },
    }
    files = {("owner/name", REV, "config.json"): SMALL}
    return repos, files


def skeleton() -> dict:
    return {
        "schema": 1, "id": "fake-entry", "display_name": "Fake",
        "source": {"repo": "owner/name", "revision": REV, "license": "MIT", "modification": "none"},
        "artifacts": [
            {"path": "weights.bin", "bytes": None, "role": "weight"},
            {"path": "config.json", "bytes": None, "role": "config"},
            {"repo": "other/base", "revision": BASE_REV, "path": "shard", "bytes": None, "role": "engram-source"},
        ],
        "staging": [
            {"step": "download", "artifacts": "all"},
            {"step": "join", "inputs": ["weight"], "output_bytes": None, "delete_inputs_after": True},
        ],
        "profile": {
            "engine": "llamacpp", "engine_lock": "llamacpp-server-cuda",
            "gpu": {"vendor": "nvidia", "count": 1, "vram_gb_each": 8},
            "host_ram_gb": 16, "storage": {"random_read_required": False},
            "context_tokens": 4096, "concurrency": 1, "port": 8080,
            "startup_hours_estimate": 0.25, "min_download_mbps": 500,
        },
        "sources": [{"url": "https://example.org/a", "covers": ["profile.*"]}],
        "tests": [], "status": "unverified",
    }


def test_fill_writes_sizes_and_hashes_and_the_result_verifies():
    repos, files = make_world()
    client = FakeClient(repos, files)
    filled = ve.fill_entry(skeleton(), client)
    weights, config, shard = filled["artifacts"]
    assert weights == {"path": "weights.bin", "bytes": 5000, "sha256": LFS_SHA, "role": "weight"}
    assert config["bytes"] == len(SMALL)
    assert config["git_blob_id"] == ve.git_blob_id(SMALL)
    assert config["sha256"] == hashlib.sha256(SMALL).hexdigest()
    assert shard["bytes"] == 7000 and shard["repo"] == "other/base"
    assert filled["staging"][1]["output_bytes"] == 5000  # a join writes the sum of its inputs
    assert ve.verify_online(filled, client) == []


def test_verify_reports_a_size_mismatch():
    repos, files = make_world()
    client = FakeClient(repos, files)
    filled = ve.fill_entry(skeleton(), client)
    filled["artifacts"][0]["bytes"] += 1
    problems = ve.verify_online(filled, client)
    assert any("size is 5001 but Hugging Face reports 5000" in p for p in problems)


def test_verify_reports_a_wrong_lfs_hash():
    repos, files = make_world()
    client = FakeClient(repos, files)
    filled = ve.fill_entry(skeleton(), client)
    filled["artifacts"][0]["sha256"] = "0" * 64
    assert any("sha256 does not match the LFS record" in p for p in ve.verify_online(filled, client))


def test_verify_reports_a_wrong_blob_id_and_wrong_content():
    repos, files = make_world()
    client = FakeClient(repos, files)
    filled = ve.fill_entry(skeleton(), client)
    wrong = copy.deepcopy(filled)
    wrong["artifacts"][1]["git_blob_id"] = "0" * 40
    assert any("git_blob_id does not match" in p for p in ve.verify_online(wrong, client))

    files[("owner/name", REV, "config.json")] = b"tampered"
    assert any("does not match the Git blob ID" in p for p in ve.verify_online(filled, FakeClient(repos, files)))


def test_verify_reports_a_missing_file_and_a_missing_repo():
    repos, files = make_world()
    client = FakeClient(repos, files)
    filled = ve.fill_entry(skeleton(), client)
    repos[("owner/name", REV)]["siblings"] = [s for s in repos[("owner/name", REV)]["siblings"] if s["rfilename"] != "weights.bin"]
    assert any("the file is missing at that revision" in p for p in ve.verify_online(filled, client))

    del repos[("other/base", BASE_REV)]
    assert any("cannot read the repo" in p for p in ve.verify_online(filled, FakeClient(repos, files)))


def test_verify_reports_a_moved_revision_and_a_gated_repo():
    repos, files = make_world()
    client = FakeClient(repos, files)
    filled = ve.fill_entry(skeleton(), client)
    repos[("owner/name", REV)]["sha"] = "3" * 40
    repos[("owner/name", REV)]["gated"] = True
    problems = ve.verify_online(filled, FakeClient(repos, files))
    assert any("the API reports revision" in p for p in problems)
    assert any("gated or private" in p for p in problems)


def test_unfilled_sizes_are_a_failure_online():
    repos, files = make_world()
    problems = ve.verify_online(skeleton(), FakeClient(repos, files))
    assert any("Run tools/verify_entry.py --fill" in p for p in problems)


def test_license_disagreement_is_reported():
    repos, files = make_world()
    repos[("owner/name", REV)]["cardData"] = {"license": "apache-2.0"}
    client = FakeClient(repos, files)
    filled = ve.fill_entry(skeleton(), client)
    assert any("model card says apache-2.0" in p for p in ve.verify_online(filled, client))


def test_llamacpp_architecture_check_uses_the_lock_list(capsys):
    import struct

    def string(text):
        raw = text.encode()
        return struct.pack("<Q", len(raw)) + raw

    header = (b"GGUF" + struct.pack("<I", 3) + struct.pack("<Q", 0) + struct.pack("<Q", 1)
              + string("general.architecture") + struct.pack("<I", 8) + string("mystery"))
    repos, files = make_world()
    files[("owner/name", REV, "weights.bin")] = header + b"\0" * 100
    data = skeleton()
    data["artifacts"][0]["path"] = "weights.bin"
    client = FakeClient(repos, files)
    filled = ve.fill_entry(data, client)
    filled["artifacts"][0]["path"] = "weights.bin"
    # An empty list skips the check and says so.
    assert ve._check_gguf_architecture(filled, client, {"supported_architectures": []}) == []
    assert "skipped" in capsys.readouterr().err
    # The weight path must end in .gguf to count, so rename it for the check.
    gg = copy.deepcopy(filled)
    gg["artifacts"][0]["path"] = "weights.gguf"
    files[("owner/name", REV, "weights.gguf")] = header
    problems = ve._check_gguf_architecture(gg, client, {"supported_architectures": ["llama"]})
    assert any("'mystery' is not in the pinned llama.cpp build's list" in p for p in problems)
    assert ve._check_gguf_architecture(gg, client, {"supported_architectures": ["mystery"]}) == []


def test_main_fill_rewrites_the_file_in_canonical_form(tmp_path: Path, capsys):
    repos, files = make_world()
    client = FakeClient(repos, files)
    path = tmp_path / "fake-entry.yaml"
    from ablit.catalog.dump import dump_entry

    path.write_text(dump_entry(skeleton(), ["# header kept"]), encoding="utf-8")
    assert ve.main([str(path), "--fill"], client=client) == 0
    text = path.read_text(encoding="utf-8")
    assert text.startswith("# header kept\n")
    data = yaml.safe_load(text)
    assert data["artifacts"][0]["bytes"] == 5000
    assert ve.main([str(path)], client=client) == 0
    assert ve.main([str(path), "--offline"]) == 0
    out = capsys.readouterr().out
    assert "filled fake-entry.yaml" in out and "(offline checks only)" in out


def test_main_reports_failure_with_a_nonzero_exit(tmp_path: Path):
    repos, files = make_world()
    client = FakeClient(repos, files)
    path = tmp_path / "fake-entry.yaml"
    from ablit.catalog.dump import dump_entry

    data = ve.fill_entry(skeleton(), client)
    data["artifacts"][0]["bytes"] = 1
    path.write_text(dump_entry(data), encoding="utf-8")
    assert ve.main([str(path)], client=client) == 1


def test_git_blob_id_matches_git():
    # `printf 'what is up, doc?' | git hash-object --stdin` gives this value.
    assert ve.git_blob_id(b"what is up, doc?") == "bd9dbf5aae1a3862dd1526723246b20206e5fc37"


def test_the_client_refuses_other_hosts_and_plain_http():
    client = ve.HFClient(retries=1)
    with pytest.raises(ValueError):
        client._get("https://example.org/anything")
    with pytest.raises(ValueError):
        client._get("http://huggingface.co/api/models/x")


def test_allowed_hosts():
    assert ve._host_allowed("huggingface.co")
    assert ve._host_allowed("cas-bridge.xethub.hf.co")
    assert not ve._host_allowed("evilhuggingface.co")
    assert not ve._host_allowed("huggingface.co.evil.example")
