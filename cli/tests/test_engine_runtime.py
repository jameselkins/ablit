"""Exercise the reviewed shell functions with local synthetic files."""

import gzip
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
        assert set(archive.getnames()) == {"env", "artifacts.tsv", "engine/bootstrap.sh", "engine/health.sh", "engine/common.sh",
                                           "engine/fetch.py", "ablit_watchdog.py"}
        env = archive.extractfile("env").read().decode()
        assert "ENDPOINT_KEY=" not in env and "VAST_API_KEY=" not in env
        assert "ABLIT_MIN_VRAM_MB_EACH=" in env
        artifacts = archive.extractfile("artifacts.tsv").read().decode()
        assert entry.source.revision in artifacts
        assert entry.artifacts[0].sha256 in artifacts


def test_a_xolotl_bundle_ships_its_own_scripts_the_downloader_and_the_lock_checks():
    store = RunStore()
    record = saved_run(store)
    entry = resolve(load_catalog().entries, "ds41-september-gsq-uncensored")
    lock = engines.engine_lock(entry)
    with tarfile.open(fileobj=io.BytesIO(engines.bundle(entry, record))) as archive:
        assert set(archive.getnames()) == {"env", "artifacts.tsv", "engine/bootstrap.sh", "engine/health.sh", "engine/common.sh",
                                           "engine/fetch.py", "ablit_watchdog.py"}
        assert b"xolotl" in archive.extractfile("engine/bootstrap.sh").read().lower()
        env = dict(line.split("=", 1) for line in archive.extractfile("env").read().decode().splitlines())
        tsv = [line.split("\t") for line in archive.extractfile("artifacts.tsv").read().decode().splitlines()]
    env = {key: shlex.split(value)[0] for key, value in env.items()}
    assert env["ABLIT_CUDA_ARCHITECTURES"] == "86 89 120"
    assert env["ABLIT_MIN_DRIVER_MAJOR"] == str(lock["min_driver_major"])
    assert env["ABLIT_MIN_MEMLOCK_KB"] == str(lock["min_memlock_kb"])
    # The staging plan joins the parts one at a time and deletes each, and no step extracts the tables.
    assert env["ABLIT_DELETE_PARTS"] == "1" and env["ABLIT_DELETE_ENGRAM_SOURCE"] == "0"
    # The box must hold the staging peak plus the margin for the join.
    peak = engines.compute_staging(entry).peak_gb
    assert float(env["ABLIT_MIN_DISK_GB"]) == pytest.approx(peak + lock["disk_margin_gb"])
    assert float(env["ABLIT_MIN_DOWNLOAD_MBPS"]) == record["min_download_mbps"]
    assert "ENDPOINT_KEY=" not in "".join(env) and "VAST_API_KEY" not in "".join(env)
    # Every artifact line carries a url, a path, a size, a sha256 and a role the downloader knows.
    assert all(len(row) == 5 and len(row[3]) == 64 for row in tsv)
    assert {row[4] for row in tsv} >= {"weight", "engram-source", "engram-index", "tokenizer"}


def test_the_llamacpp_bundle_has_no_xolotl_only_settings():
    store = RunStore()
    record = saved_run(store)
    entry = resolve(load_catalog().entries, MODEL)
    with tarfile.open(fileobj=io.BytesIO(engines.bundle(entry, record))) as archive:
        env = archive.extractfile("env").read().decode()
    assert "ABLIT_MIN_MEMLOCK_KB" not in env and "ABLIT_DELETE_PARTS" not in env


def test_a_bundle_for_an_engine_without_a_bootstrap_is_refused():
    store = RunStore()
    record = saved_run(store)
    entry = resolve(load_catalog().entries, "ds41-1017-abl-fp8")
    with pytest.raises(ValueError, match="no bootstrap for the vllm engine"):
        engines.bundle(entry, record)


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


@pytest.mark.parametrize("mode", ["buffered", "streamed", "gzip"])
def test_health_exercises_models_chat_stream_and_auth_with_synthetic_answers(mode):
    base = "http://127.0.0.1:18001"
    body = (
        'data: {"choices": [{"delta": {"content": "x"}}]}\n\n'
        'data: {"choices": [{"delta": {}, "finish_reason": "stop"}]}\n\n'
        'data: {"choices": [], "usage": {"completion_tokens": 1}}\n\n'
        'data: [DONE]\n\n'
    ).encode()
    wire_body = gzip.compress(body) if mode == "gzip" else body

    class CompleteStream(httpx.SyncByteStream):
        def __iter__(self):
            yield wire_body[:5]
            yield wire_body[5:]

    streamed = (httpx.Response(200, content=body) if mode == "buffered" else
                httpx.Response(200, stream=CompleteStream(), headers={"Content-Encoding": "gzip"} if mode == "gzip" else {}))
    with respx.mock as router:
        models = router.get(base + "/v1/models").mock(side_effect=[
            httpx.Response(200, json={"data": [{"id": "invented-model"}]}),
            httpx.Response(401), httpx.Response(403)])
        chat = router.post(base + "/v1/chat/completions").mock(side_effect=[
            httpx.Response(200, json={"choices": [{"message": {"content": "x"}}]}),
            streamed])
        engines.health(base, "invented-endpoint-key", "invented-model")
        assert models.call_count == 3 and chat.call_count == 2
        assert models.calls[0].request.headers["authorization"] == "Bearer invented-endpoint-key"
        assert "authorization" not in models.calls[1].request.headers


@pytest.mark.parametrize("compressed", [False, True])
@pytest.mark.parametrize("extra_bytes", [0, 1])
def test_health_stream_limits_decoded_utf8_bytes(compressed, extra_bytes):
    base = "http://127.0.0.1:18001"
    tail = b'data: {"choices": [{"delta": {"content": "x"}}]}\n\ndata: [DONE]\n\n'
    size = 1_048_576 + extra_bytes
    padding_size = size - len(tail) - 4
    padding = ("é" * (padding_size // 2)).encode() + b"x" * (padding_size % 2)
    body = b": " + padding + b"\n\n" + tail
    assert len(body) == size
    wire_body = gzip.compress(body) if compressed else body
    closed = []

    class SizedStream(httpx.SyncByteStream):
        def __iter__(self):
            middle = len(wire_body) // 2
            yield wire_body[:middle]
            yield wire_body[middle:]

        def close(self):
            closed.append(True)

    with respx.mock as router:
        router.get(base + "/v1/models").mock(side_effect=[
            httpx.Response(200, json={"data": [{"id": "invented-model"}]}),
            httpx.Response(401), httpx.Response(403)])
        router.post(base + "/v1/chat/completions").mock(side_effect=[
            httpx.Response(200, json={"choices": [{"message": {"content": "x"}}]}),
            httpx.Response(200, stream=SizedStream(), headers={"Content-Encoding": "gzip"} if compressed else {})])
        if extra_bytes:
            with pytest.raises(ValueError, match="response size limit"):
                engines.health(base, "invented-endpoint-key", "invented-model")
        else:
            engines.health(base, "invented-endpoint-key", "invented-model")
    assert closed


@pytest.mark.parametrize("compressed", [False, True])
@pytest.mark.parametrize("prefix,tail", [
    ([b": heartbeat\n\n", b": heartbeat\n\n"],
     b'data: {"choices": [{"delta": {"content": "x"}}]}\n\ndata: [DONE]\n\n'),
    ([b"data: ", b" "],
     b'{"choices": [{"delta": {"content": "x"}}]}\n\ndata: [DONE]\n\n'),
])
def test_health_stream_deadline_includes_heartbeats_and_partial_lines(monkeypatch, prefix, tail, compressed):
    import time
    base = "http://127.0.0.1:18001"
    clock, received, closed = [0.0], [], []
    if compressed:
        body = gzip.compress(b"".join(prefix) + tail)
        prefix, tail = [body[:5], body[5:10]], body[10:]

    class SlowStream(httpx.SyncByteStream):
        def __iter__(self):
            for part in prefix:
                clock[0] += 60
                received.append(part)
                yield part
            received.append(tail)
            yield tail

        def close(self):
            closed.append(True)

    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    with respx.mock as router:
        router.get(base + "/v1/models").mock(side_effect=[
            httpx.Response(200, json={"data": [{"id": "invented-model"}]}),
            httpx.Response(401), httpx.Response(403)])
        router.post(base + "/v1/chat/completions").mock(side_effect=[
            httpx.Response(200, json={"choices": [{"message": {"content": "x"}}]}),
            httpx.Response(200, stream=SlowStream(), headers={"Content-Encoding": "gzip"} if compressed else {})])
        with pytest.raises(ValueError, match="within 120 seconds"):
            engines.health(base, "invented-endpoint-key", "invented-model")
    assert len(received) == 2
    assert closed


@pytest.mark.parametrize("response", [
    None,
    [],
    {"data": []},
    {"data": [None]},
    {"data": ["invented-model"]},
    {"data": [{}]},
    {"data": [{"id": ""}]},
    {"data": [{"id": 7}]},
    {"data": [{"id": True}]},
    {"error": {"message": "invented failure"}, "data": [{"id": "invented-model"}]},
    {"data": [{"id": "invented-model"}, {}]},
])
def test_health_rejects_invalid_model_listings(response):
    base = "http://127.0.0.1:18001"
    with respx.mock(assert_all_called=False) as router:
        router.get(base + "/v1/models").mock(side_effect=[
            httpx.Response(200, text=json.dumps(response)), httpx.Response(401), httpx.Response(403)])
        chat = router.post(base + "/v1/chat/completions").mock(side_effect=[
            httpx.Response(200, json={"choices": [{"message": {"content": "x"}}]}),
            httpx.Response(200, text='data: {"choices": [{"delta": {"content": "x"}}]}\n\ndata: [DONE]\n\n')])
        with pytest.raises(ValueError, match="models response"):
            engines.health(base, "invented-endpoint-key", "invented-model")
        assert chat.call_count == 0


@pytest.mark.parametrize("response", [
    None,
    [],
    {"choices": []},
    {"choices": "x"},
    {"choices": {"message": {"content": "x"}}},
    {"choices": [None]},
    {"choices": [{"delta": {"content": "x"}}]},
    {"choices": [{"message": "x"}]},
    {"error": {"message": "invented failure"}, "choices": [{"message": {"content": "x"}}]},
    {"choices": [{"message": {"content": "x"}}, None]},
])
def test_health_rejects_invalid_chat_completion_responses(response):
    base = "http://127.0.0.1:18001"
    with respx.mock as router:
        router.get(base + "/v1/models").mock(side_effect=[
            httpx.Response(200, json={"data": [{"id": "invented-model"}]}),
            httpx.Response(401), httpx.Response(403)])
        router.post(base + "/v1/chat/completions").mock(side_effect=[
            httpx.Response(200, text=json.dumps(response)),
            httpx.Response(200, text='data: {"choices": [{"delta": {"content": "x"}}]}\n\ndata: [DONE]\n\n')])
        with pytest.raises(ValueError, match="completion response"):
            engines.health(base, "invented-endpoint-key", "invented-model")


@pytest.mark.parametrize("stream", [
    "data: [DONE]\n\n",
    ": heartbeat\n\ndata: [DONE]\n\n",
    "data: [DONE]\n\ndata: [DONE]\n\n",
    'data: {"choices": [{"delta": {"content": "x"}}]}\n\n',
    'data: {"error": {"message": "invented stream failure"}}\n\ndata: [DONE]\n\n',
    'data: []\n\ndata: [DONE]\n\n',
    'data: {"choices": [], "usage": {"completion_tokens": 0}}\n\ndata: [DONE]\n\n',
    'data: {"choices": [{"message": {"content": "x"}}]}\n\ndata: [DONE]\n\n',
    'data: {"choices": [{"delta": "x"}]}\n\ndata: [DONE]\n\n',
    'data: {"choices": [null]}\n\ndata: [DONE]\n\n',
    'data: not-json\n\ndata: [DONE]\n\n',
    ('data: {"choices": [{"delta": {"content": "x"}}]}\n\n'
     'data: {"error": {"message": "invented stream failure"}}\n\ndata: [DONE]\n\n'),
])
def test_health_requires_a_stream_chunk_and_the_end_marker(stream):
    base = "http://127.0.0.1:18001"
    with respx.mock as router:
        router.get(base + "/v1/models").mock(side_effect=[
            httpx.Response(200, json={"data": [{"id": "invented-model"}]}),
            httpx.Response(401), httpx.Response(403)])
        router.post(base + "/v1/chat/completions").mock(side_effect=[
            httpx.Response(200, json={"choices": [{"message": {"content": "x"}}]}),
            httpx.Response(200, text=stream)])
        with pytest.raises(ValueError, match="stream"):
            engines.health(base, "invented-endpoint-key", "invented-model")


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


def test_an_engine_that_exits_on_start_fails_the_stage_with_its_log(repo_root, tmp_path):
    common = repo_root / "engines/lib/common.sh"
    script = '''set -euo pipefail
source "$1"
ablit_start_engine INVENTED_KEY bash -c 'echo "error while loading shared libraries"; exit 127'
'''
    result = subprocess.run(["bash", "-c", script, "test", str(common)],
                            env={**os.environ, "ABLIT_STATE_DIR": str(tmp_path), "ABLIT_ENDPOINT_KEY": "invented-endpoint-key",
                                 "ABLIT_ENGINE_START_CHECK_S": "1"}, capture_output=True)
    assert result.returncode != 0
    failed = json.loads((tmp_path / "failed.json").read_text())
    assert failed["reason"] == "engine_exited" and "shared libraries" in failed["detail"]
