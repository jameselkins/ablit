"""Run engines/xolotl/bootstrap.sh against a local server and a stand-in for the engine image.

The stand-ins are small Python scripts that keep the shape of the real ones: the
server needs an API key in its environment, reads the config file, opens the
Engram folders the way the engine does, and listens only after a short load time.
Nothing here starts a real engine or touches a network beyond 127.0.0.1.
"""

from __future__ import annotations

import json
import os
import re
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from test_fetch import SMALL, FileServer, payload, sha

REPO = Path(__file__).resolve().parents[2]
BOOTSTRAP = REPO / "engines" / "xolotl" / "bootstrap.sh"

FAKE_SERVER = r'''
import json, os, sys, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

args = sys.argv[1:]
def option(name):
    return args[args.index(name) + 1]

key = os.environ.get("STRATA_API_KEY", "")
if not key:
    print("[strata] an API key was given but it is empty", file=sys.stderr)
    sys.exit(2)
config = json.load(open(option("--config")))
engine_args = config["args"]
engram = engine_args[engine_args.index("--engram") + 1]
with open(os.environ["FAKE_RECORD"], "a") as handle:
    handle.write(json.dumps({"engram": engram, "host": option("--host"), "port": int(option("--port")),
                             "model": engine_args[engine_args.index("-m") + 1], "text_log": os.environ.get("STRATA_TEXT_LOG"),
                             "key_seen": bool(key)}) + "\n")
if os.environ.get("FAKE_DIRECT_FAILS") and "layers." not in engram:
    print("engram: layers.1.engram.embed.weight has an unexpected dtype/size", file=sys.stderr, flush=True)
    sys.exit(1)
for folder in engram.split(":"):
    if not os.path.isdir(folder):
        print("engram: cannot open " + folder, file=sys.stderr, flush=True)
        sys.exit(1)
time.sleep(float(os.environ.get("FAKE_LOAD_SECONDS", "0.4")))

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"{}")
    def log_message(self, *a):
        pass

ThreadingHTTPServer((option("--host"), int(option("--port"))), Handler).serve_forever()
'''

FAKE_EXTRACTOR = r'''
import json, os, sys, time

args = sys.argv[1:]
source = args[0]
routes = {}
i = 1
while i < len(args):
    if args[i] == "--to":
        key, value = args[i + 1].split("=", 1)
        routes[key] = value
        i += 2
    else:
        i += 1
for name in os.environ["FAKE_SHARDS"].split(","):
    if not os.path.exists(os.path.join(source, name)):
        sys.exit("shard not found: " + name)
if not os.path.exists(os.path.join(source, "model.safetensors.index.json")):
    sys.exit("no model.safetensors.index.json in " + source)
with open(os.environ["FAKE_EXTRACT_STARTED"], "w") as handle:
    handle.write(source)
time.sleep(0.3)
for key, folder in routes.items():
    with open(os.path.join(folder, "engram-L" + key.split(".")[1] + ".safetensors"), "wb") as handle:
        handle.write(b"x" * 1000)
    with open(os.path.join(folder, "model.safetensors.index.json"), "w") as handle:
        json.dump({"weight_map": {}}, handle)
'''

SHARDS = ["model-00047-of-00048.safetensors", "model-00048-of-00048.safetensors"]
PARTS = ["v/part-01-of-04", "v/part-02-of-04", "v/part-03-of-04", "v/part-04-of-04"]


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class Box:
    """One simulated rented machine: folders, a fake image, and a download list."""

    def __init__(self, tmp: Path, parts: list[str] | None = None) -> None:
        self.tmp = tmp
        self.data, self.state = tmp / "data", tmp / "state"
        self.data.mkdir()
        self.state.mkdir()
        self.install = tmp / "opt-xolotl"
        for folder in ("build-xolotl", "bin", "tools/v41/serve"):
            (self.install / folder).mkdir(parents=True)
        for path in ("build-xolotl/ds41_generate", "bin/ds41-engine"):
            (self.install / path).write_text("#!/bin/sh\nexit 0\n")
            (self.install / path).chmod(0o755)
        (self.install / "tools/v41/serve/serve_ds41.py").write_text(FAKE_SERVER)
        (self.install / "tools/v41/extract_engram.py").write_text(FAKE_EXTRACTOR)
        self.record = tmp / "launches.jsonl"
        self.extract_started = tmp / "extract-started"
        self.port = free_port()
        part_names = parts or PARTS
        sizes = [60_000] * (len(part_names) - 1) + [20_000] if len(part_names) > 1 else [50_000]
        self.files: dict[str, tuple[bytes, str]] = {}
        for name, size in zip(part_names, sizes, strict=False):
            self.files[name] = (payload(name, size), "weight")
        self.files["v/experts.json"] = (payload("experts", 700), "expert-map")
        self.files["v/SHA256SUMS"] = (payload("sums", 300), "checksum")
        self.files["v/reassemble.sh"] = (payload("script", 400), "script")
        self.files["v/rank.bin"] = (payload("rank", 4096), "rank")
        for name in ("vocab.json", "merges.txt"):
            self.files["tokenizer/" + name] = (payload(name, 900), "tokenizer")
        self.files["draft.gguf"] = (payload("draft", 40_000), "draft")
        self.files["model.safetensors.index.json"] = (payload("index", 3_000), "engram-index")
        for name in SHARDS:
            self.files[name] = (payload(name, 50_000), "engram-source")
        self.joined = self.data / "joined" / "model.gguf"
        self.weights = b"".join(self.files[name][0] for name in part_names)

    def write_list(self, server: FileServer) -> None:
        rows = [f"{server.url(name)}\t{name}\t{len(data)}\t{sha(data)}\t{role}\n"
                for name, (data, role) in self.files.items()]
        (self.tmp / "artifacts.tsv").write_text("".join(rows))

    def server_files(self) -> dict[str, bytes]:
        return {name: data for name, (data, _role) in self.files.items()}

    def env(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        return {
            **os.environ, **SMALL,
            "ABLIT_DATA_DIR": str(self.data), "ABLIT_STATE_DIR": str(self.state),
            "ABLIT_ARTIFACTS_TSV": str(self.tmp / "artifacts.tsv"),
            "XOLOTL_DIR": str(self.install), "XOLOTL_PYTHON": sys.executable,
            "ABLIT_ENDPOINT_KEY": "invented-endpoint-key", "ABLIT_PORT": str(self.port),
            "ABLIT_SERVED_NAME": "invented-model", "ABLIT_CONTEXT_TOKENS": "4096",
            "ABLIT_READY_TIMEOUT_S": "30", "ABLIT_DELETE_PARTS": "1",
            "ABLIT_DOWNLOAD_LOG_SECONDS": "5",
            "ABLIT_WAIT_POLL_S": "0.1", "ABLIT_LISTENER_SLEEP_S": "0.1", "ABLIT_HEALTH_SLEEP_S": "0.1",
            "ABLIT_ENGINE_START_CHECK_S": "1",
            "FAKE_RECORD": str(self.record), "FAKE_EXTRACT_STARTED": str(self.extract_started),
            "FAKE_SHARDS": ",".join(SHARDS),
            **(extra or {}),
        }

    def run(self, script: str, extra: dict[str, str] | None = None, timeout: int = 90):
        return subprocess.run(["bash", "-c", "set -euo pipefail\nsource \"$1\"\n" + script, "test", str(BOOTSTRAP)],
                              env=self.env(extra), capture_output=True, text=True, timeout=timeout)

    def run_main(self, extra: dict[str, str] | None = None, stages: str = "", timeout: int = 90):
        """Run main with the stages that need a real GPU replaced by no-ops."""
        script = (stages or "stage_start() { :; }; stage_check() { :; }; stage_randread() { :; }; stage_speed() { :; }; "
                  "stage_health() { :; }\nablit_free_bytes() { echo 99999999999999; }\nmain\n")
        return self.run(script, extra, timeout)

    def launches(self) -> list[dict]:
        if not self.record.exists():
            return []
        return [json.loads(line) for line in self.record.read_text().splitlines()]

    def failed(self) -> dict:
        return json.loads((self.state / "failed.json").read_text())

    def stop_engine(self) -> None:
        pidfile = self.state / "engine.pid"
        if pidfile.exists():
            try:
                os.kill(int(pidfile.read_text()), signal.SIGKILL)
            except (ProcessLookupError, ValueError):
                pass


@pytest.fixture
def box(tmp_path):
    machine = Box(tmp_path)
    yield machine
    machine.stop_engine()


def test_the_default_run_joins_while_downloading_and_reads_the_engram_files_in_place(box):
    gates = {PARTS[3]: lambda: box.joined.exists() and box.joined.stat().st_size >= 120_000}
    with FileServer(box.server_files(), gates=gates, gate_timeout=25) as server:
        box.write_list(server)
        done = box.run_main()
        assert done.returncode == 0, done.stderr
        # The last part waited until the join had already appended two parts.
        assert server.gate_results == {PARTS[3]: True}
    assert box.joined.read_bytes() == box.weights
    assert not any((box.data / name).exists() for name in PARTS), "parts are deleted after the join"
    for name in SHARDS + ["model.safetensors.index.json", "draft.gguf", "v/rank.bin", "tokenizer/vocab.json"]:
        assert (box.data / name).read_bytes() == box.files[name][0]
    assert not box.extract_started.exists(), "direct mode copies nothing"
    assert (box.state / "steps" / "join.done").exists() and not (box.state / "steps" / "extract.done").exists()
    assert (box.state / "consumed" / "v%2Fpart-01-of-04").exists()
    assert json.loads((box.state / "progress" / "join.json").read_text())["bytes_done"] == len(box.weights)
    assert json.loads((box.state / "ready.json").read_text())["port"] == box.port
    launch = box.launches()
    assert len(launch) == 1
    assert launch[0]["engram"] == str(box.data) and launch[0]["host"] == "127.0.0.1"
    assert launch[0]["key_seen"] is True and launch[0]["text_log"] == "0"
    assert launch[0]["model"] == str(box.joined)
    config_text = (box.state / "xolotl-config.json").read_text()
    assert "invented-endpoint-key" not in config_text
    assert "invented-endpoint-key" not in "".join(
        p.read_text(errors="replace") for p in (box.state / "logs").glob("*.log"))
    config = json.loads(config_text)
    assert config["exe"] == str(box.install / "bin" / "ds41-engine") and config["host"] == "127.0.0.1"
    assert config["api_monitor"] is False and config["port"] == box.port and config["model_name"] == "invented-model"
    assert config["env"]["STRATA_SERVE_DSPARK"] == str(box.data / "draft.gguf")
    assert config["tokenizer"] == str(box.data / "tokenizer")
    flags = config["args"]
    assert flags[flags.index("--rank") + 1] == str(box.data / "v" / "rank.bin")
    assert flags[flags.index("--ctx") + 1] == "4096" and flags[flags.index("--arena-gib") + 1] == "60"
    assert oct((box.state / "xolotl-config.json").stat().st_mode & 0o777) in ("0o600", "0o644")


def test_extract_mode_starts_the_extraction_before_the_last_weight_part_is_served(box):
    gates = {PARTS[2]: lambda: box.extract_started.exists()}
    with FileServer(box.server_files(), gates=gates, gate_timeout=25) as server:
        box.write_list(server)
        done = box.run_main({"ABLIT_ENGRAM_MODE": "extract", "ABLIT_DELETE_ENGRAM_SOURCE": "1"})
        assert done.returncode == 0, done.stderr
        assert server.gate_results == {PARTS[2]: True}
    assert box.joined.read_bytes() == box.weights
    assert (box.state / "steps" / "extract.done").exists()
    for layer in ("1", "14"):
        assert (box.data / "engram" / f"layers.{layer}" / f"engram-L{layer}.safetensors").exists()
    assert not any((box.data / name).exists() for name in SHARDS), "sources are deleted after extraction"
    assert (box.state / "consumed" / SHARDS[0]).exists()
    launch = box.launches()
    assert len(launch) == 1
    assert launch[0]["engram"] == f"{box.data}/engram/layers.1:{box.data}/engram/layers.14"
    assert json.loads((box.state / "progress" / "extract.json").read_text())["bytes_total"] == 100_000


def test_the_engine_falls_back_to_extraction_when_it_cannot_open_the_files_in_place(box):
    with FileServer(box.server_files()) as server:
        box.write_list(server)
        done = box.run_main({"FAKE_DIRECT_FAILS": "1"})
        assert done.returncode == 0, done.stderr
    launches = box.launches()
    assert [item["engram"] == str(box.data) for item in launches] == [True, False]
    assert launches[1]["engram"].endswith("layers.14") and "layers.1:" in launches[1]["engram"]
    assert box.extract_started.exists() and (box.state / "steps" / "extract.done").exists()
    assert not (box.state / "failed.json").exists()
    # The shards stay, because the staging plan did not ask to delete them.
    assert all((box.data / name).exists() for name in SHARDS)


def test_a_failed_start_that_is_not_about_the_engram_files_fails_the_run_with_the_log(box):
    broken = box.install / "tools/v41/serve/serve_ds41.py"
    broken.write_text("import sys\nprint('CUDA error: no kernel image is available', file=sys.stderr)\nsys.exit(1)\n")
    with FileServer(box.server_files()) as server:
        box.write_list(server)
        done = box.run_main()
    assert done.returncode == 6
    info = box.failed()
    assert info["reason"] == "engine_exited" and "no kernel image" in info["detail"]
    assert not box.extract_started.exists(), "no extraction for an error that has nothing to do with Engram"


def test_extraction_fails_clearly_when_the_disk_is_too_small(box):
    with FileServer(box.server_files()) as server:
        box.write_list(server)
        done = box.run_main({"FAKE_DIRECT_FAILS": "1"}, stages=(
            "stage_start() { :; }; stage_check() { :; }; stage_randread() { :; }; stage_speed() { :; }; stage_health() { :; }\n"
            "ablit_free_bytes() { echo 1000; }\nmain\n"))
    assert done.returncode == 6
    assert box.failed()["reason"] == "disk"
    assert not box.extract_started.exists()


def test_a_wrong_hash_stops_the_run_and_the_other_steps(box):
    with FileServer(box.server_files(), corrupt=PARTS[1]) as server:
        box.write_list(server)
        done = box.run_main()
    assert done.returncode == 6
    assert box.failed()["reason"] == "hash_mismatch"
    assert not box.launches()
    assert not (box.state / "stages" / "07-engine.done").exists()


def test_a_failed_join_ends_the_run_while_the_download_is_still_going(box):
    # A file where the joined folder should be makes the join worker fail at once.
    (box.data / "joined").write_text("in the way")
    with FileServer(box.server_files(), delay_per_block=0.5) as server:
        box.write_list(server)
        started = time.monotonic()
        done = box.run_main()
        elapsed = time.monotonic() - started
    assert done.returncode == 6
    assert (box.state / "failed.json").exists()
    assert not any((box.data / name).exists() for name in SHARDS), "the download stopped before the last files"
    assert elapsed < 60


def test_one_weight_file_is_renamed_not_copied(tmp_path):
    machine = Box(tmp_path, parts=["v/model-only.gguf"])
    try:
        with FileServer(machine.server_files()) as server:
            machine.write_list(server)
            done = machine.run_main()
            assert done.returncode == 0, done.stderr
        assert machine.joined.read_bytes() == machine.files["v/model-only.gguf"][0]
        assert not (machine.data / "v" / "model-only.gguf").exists()
        assert (machine.state / "consumed" / "v%2Fmodel-only.gguf").exists()
    finally:
        machine.stop_engine()


def test_a_second_run_downloads_nothing_and_leaves_a_live_engine_alone(box):
    with FileServer(box.server_files()) as server:
        box.write_list(server)
        assert box.run_main().returncode == 0
        requests = len(server.requests)
        again = box.run_main()
        assert again.returncode == 0, again.stderr
        assert len(server.requests) == requests
        assert len(box.launches()) == 1
        # A dead engine is started again, and the files are not fetched again.
        box.stop_engine()
        time.sleep(0.5)
        third = box.run_main()
        assert third.returncode == 0, third.stderr
        assert len(box.launches()) == 2 and len(server.requests) == requests


def test_the_image_is_checked_before_any_download(box):
    (box.install / "build-xolotl" / "ds41_generate").unlink()
    done = box.run("stage_check", )
    assert done.returncode == 6
    info = box.failed()
    assert info["reason"] == "missing_file" and "ds41_generate" in info["detail"]


def test_a_wrong_engram_mode_is_refused(box):
    done = box.run("stage_check", {"ABLIT_ENGRAM_MODE": "sideways"})
    assert done.returncode == 6 and box.failed()["reason"] == "bad_variable"


def run_order(box, mode: str) -> list[str]:
    script = '''ablit_download_all() { echo "$ABLIT_DOWNLOAD_FILES $ABLIT_DOWNLOAD_RANGES $*"; }
download_job
'''
    done = box.run(script, {"ABLIT_ENGRAM_MODE": mode})
    assert done.returncode == 0, done.stderr
    return done.stdout.splitlines()


def test_downloads_run_in_priority_order_with_work_ahead_of_files_that_need_none(box):
    direct = run_order(box, "direct")
    assert direct == [
        "8 1 engram-index tokenizer rank config expert-map checksum script",
        "2 16 weight draft",
        "2 16 engram-source engram",
        "2 16 ",
    ]
    extract = run_order(box, "extract")
    assert extract[1:3] == ["2 16 engram-source engram", "2 16 weight draft"]


def test_the_speed_check_measures_the_first_heavy_files(box):
    script = '''ablit_speed_check() { echo "$ABLIT_DOWNLOAD_FILES $ABLIT_DOWNLOAD_RANGES $*"; }
stage_speed
'''
    assert box.run(script).stdout.split("\n")[0] == "2 16 weight draft"
    assert box.run(script, {"ABLIT_ENGRAM_MODE": "extract"}).stdout.split("\n")[0] == "2 16 engram-source engram"


def test_the_script_names_its_paths_once_and_has_no_build_on_the_box():
    text = BOOTSTRAP.read_text()
    for name in ("XOLOTL_DIR", "XOLOTL_BINARY", "XOLOTL_ENGINE_EXE", "XOLOTL_SERVER", "XOLOTL_EXTRACTOR"):
        assert len(re.findall(rf'^{name}="\${{{name}:-', text, re.M)) == 1, name
    assert "/opt/xolotl" in text and text.count("/opt/xolotl") == 1
    assert "cmake" not in text and "git clone" not in text and "fetch-llama" not in text
    assert "TODO(step 5)" in text and "TODO(step 3)" in text


def test_the_lock_still_gives_the_workflow_exactly_one_commit_line():
    lock = (REPO / "engines" / "xolotl" / "lock.yaml").read_text()
    commits = re.findall(r"^  commit: ([0-9a-f]{40})$", lock, re.M)
    assert len(commits) == 1
    assert re.search(r"^image: ghcr.io/jameselkins/ablit-xolotl$", lock, re.M)
    assert re.search(r"^tag: 03be8a6$", lock, re.M)
    assert re.search(r"^digest: sha256:[a-f0-9]{64}$", lock, re.M)
    assert re.search(r"^cuda_architectures: \[86, 89, 120\]$", lock, re.M)
