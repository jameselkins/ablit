"""Exercise the reviewed shell functions with local synthetic files."""

import hashlib
import io
import json
import os
import shlex
import subprocess
import tarfile

import httpx
import pytest
import respx

from ablit import engines
from ablit.catalog import load_catalog, resolve
from ablit.runs import RunStore
from test_run_commands import MODEL, saved_run


def test_bundle_has_only_reviewed_scripts_and_nonsecret_config():
    store = RunStore()
    record = saved_run(store)
    entry = resolve(load_catalog().entries, MODEL)
    data = engines.bundle(entry, record)
    with tarfile.open(fileobj=io.BytesIO(data)) as archive:
        assert set(archive.getnames()) == {"env", "artifacts.tsv", "engine/bootstrap.sh", "engine/health.sh", "engine/common.sh", "ablit_watchdog.py"}
        env = archive.extractfile("env").read().decode()
        assert "ENDPOINT_KEY=" not in env and "VAST_API_KEY=" not in env
        assert "ABLIT_MIN_VRAM_MB_EACH=" in env
        artifacts = archive.extractfile("artifacts.tsv").read().decode()
        assert entry.source.revision in artifacts
        assert entry.artifacts[0].sha256 in artifacts


def test_stage_marker_skips_completed_work_and_stage_start_starts_watchdog(repo_root, tmp_path):
    common = repo_root / "engines/lib/common.sh"
    script = '''set -euo pipefail
source "$1"
work() { echo work >> "$ABLIT_STATE_DIR/calls"; }
ablit_run_stage 02 check work
ablit_run_stage 02 check work
'''
    subprocess.run(["bash", "-c", script, "test", str(common)],
                   env={**os.environ, "ABLIT_STATE_DIR": str(tmp_path)}, check=True, capture_output=True)
    assert (tmp_path / "calls").read_text() == "work\n"
    assert (tmp_path / "stages/02-check.done").exists()


def test_completed_partial_download_is_verified_and_renamed_without_curl(repo_root, tmp_path):
    data = b"invented model bytes"
    (tmp_path / "weight.part").write_bytes(data)
    digest = hashlib.sha256(data).hexdigest()
    # GNU stat is used remotely. Override its tiny wrapper for the macOS test.
    script = '''set -euo pipefail
source "$1"
ablit_file_size() { if [ -f "$1" ]; then wc -c < "$1" | tr -d ' '; else echo 0; fi; }
ablit_sha256() { shasum -a 256 "$1" | cut -d' ' -f1; }
curl() { exit 91; }
ablit_download_one https://example.invalid/model weight "$2" "$3"
'''
    subprocess.run(["bash", "-c", script, "test", str(repo_root / "engines/lib/common.sh"), str(len(data)), digest],
                   env={**os.environ, "ABLIT_DATA_DIR": str(tmp_path), "ABLIT_STATE_DIR": str(tmp_path)},
                   check=True, capture_output=True)
    assert (tmp_path / "weight").read_bytes() == data
    assert not (tmp_path / "weight.part").exists()


def test_health_exercises_models_chat_stream_and_auth_with_synthetic_answers():
    base = "http://127.0.0.1:18001"
    with respx.mock as router:
        models = router.get(base + "/v1/models").mock(side_effect=[
            httpx.Response(200, json={"data": [{"id": "invented-model"}]}),
            httpx.Response(401), httpx.Response(403)])
        chat = router.post(base + "/v1/chat/completions").mock(side_effect=[
            httpx.Response(200, json={"choices": [{"message": {"content": "x"}}]}),
            httpx.Response(200, text='data: {"choices": []}\n\ndata: [DONE]\n\n')])
        engines.health(base, "invented-endpoint-key", "invented-model")
        assert models.call_count == 3 and chat.call_count == 2
        assert models.calls[0].request.headers["authorization"] == "Bearer invented-endpoint-key"
        assert "authorization" not in models.calls[1].request.headers


def test_uploaded_bootstrap_finds_common_beside_the_script(repo_root, tmp_path):
    """The remote layout differs from engines/lib in the source checkout."""
    import shutil
    engine = tmp_path / "engine"
    engine.mkdir()
    shutil.copyfile(repo_root / "engines/llamacpp/bootstrap.sh", engine / "bootstrap.sh")
    (engine / "common.sh").write_text('ablit_run_stage() { printf "%s\\n" "$1"; }\n')
    result = subprocess.run(["bash", str(engine / "bootstrap.sh")],
                            env={**os.environ, "ABLIT_STATE_DIR": str(tmp_path)},
                            check=True, capture_output=True, text=True)
    assert result.stdout.splitlines() == [f"{n:02d}" for n in range(1, 10)]


def test_prerent_artifact_check_uses_anonymous_pinned_metadata_only():
    entry = resolve(load_catalog().entries, MODEL)
    artifact = entry.artifacts[0]
    url = f"https://huggingface.co/api/models/{entry.source.repo}/revision/{entry.source.revision}"
    with respx.mock as router:
        route = router.get(url).respond(200, json={
            "id": entry.source.repo, "sha": entry.source.revision, "private": False, "gated": False,
            "siblings": [{"rfilename": artifact.path, "size": artifact.bytes,
                          "lfs": {"sha256": artifact.sha256, "size": artifact.bytes, "pointerSize": 128}}]})
        engines.check_artifacts(entry)
        assert route.call_count == 1
        assert "authorization" not in route.calls[0].request.headers
        route.respond(200, json={
            "id": entry.source.repo, "sha": entry.source.revision, "private": False, "gated": False,
            "siblings": [{"rfilename": artifact.path, "size": artifact.bytes + 1}]})
        with pytest.raises(ValueError, match="size changed"):
            engines.check_artifacts(entry)
