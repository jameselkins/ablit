"""Exercise the shared shell functions for downloads, background steps and hardware checks.

The downloads run against a local server that serves synthetic files, through the
real functions in engines/lib/common.sh and the real engines/lib/fetch.py.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from pathlib import Path

import pytest

from test_fetch import SMALL, FileServer, payload, sha, write_list

COMMON = Path(__file__).resolve().parents[2] / "engines" / "lib" / "common.sh"


@pytest.fixture
def box(tmp_path):
    data, state = tmp_path / "data", tmp_path / "state"
    data.mkdir()
    state.mkdir()
    return tmp_path, data, state


def run_bash(script: str, box_dirs, extra_env: dict[str, str] | None = None, timeout: int = 60):
    tmp, data, state = box_dirs
    env = {
        **os.environ, **SMALL,
        "ABLIT_DATA_DIR": str(data), "ABLIT_STATE_DIR": str(state),
        "ABLIT_ARTIFACTS_TSV": str(tmp / "artifacts.tsv"),
        "ABLIT_DOWNLOAD_LOG_SECONDS": "5",
        **(extra_env or {}),
    }
    return subprocess.run(["bash", "-c", "set -euo pipefail\nsource \"$1\"\n" + script, "test", str(COMMON)],
                          env=env, capture_output=True, text=True, timeout=timeout)


def failed(state: Path) -> dict:
    return json.loads((state / "failed.json").read_text())


def listing(box_dirs, server, files: dict[str, tuple[bytes, str]]):
    write_list(box_dirs[0] / "artifacts.tsv", server, files)


# --- downloads ---------------------------------------------------------------


def test_roles_choose_and_order_the_files_and_progress_counts_the_whole_list(box):
    _tmp, data, state = box
    files = {"w.bin": (payload("w", 30_000), "weight"), "s.bin": (payload("s", 30_000), "engram-source"),
             "t.json": (b"{}\n", "tokenizer")}
    with FileServer({k: v[0] for k, v in files.items()}) as server:
        listing(box, server, files)
        one_at_a_time = {"ABLIT_DOWNLOAD_FILES": "1", "ABLIT_DOWNLOAD_RANGES": "1"}
        done = run_bash("ablit_download_all engram-source weight", box, one_at_a_time)
        assert done.returncode == 0, done.stderr
        assert (data / "s.bin").exists() and (data / "w.bin").exists() and not (data / "t.json").exists()
        paths = [p.rsplit("/", 1)[1] for (_t, p, _r) in server.requests]
        assert paths.index("s.bin") < paths.index("w.bin")
        progress = json.loads((state / "progress" / "download.json").read_text())
        assert progress["files_total"] == 3 and progress["files_done"] == 2
        assert progress["bytes_total"] == 60_003 and progress["bytes_done"] == 60_000
        # A call with no roles downloads what is left, and nothing twice.
        before = len(server.requests)
        assert run_bash("ablit_download_all", box, one_at_a_time).returncode == 0
        assert (data / "t.json").read_bytes() == b"{}\n"
        assert len(server.requests) == before + 1


def test_phase_settings_reach_the_download_helper_without_leaking(box):
    _tmp, data, _state = box
    files = {"a.bin": (payload("a", 60_000), "weight")}
    script = '''ABLIT_DOWNLOAD_FILES=3 ABLIT_DOWNLOAD_RANGES=2 ablit_download_all weight
echo "after:${ABLIT_DOWNLOAD_FILES:-unset}"'''
    with FileServer({"a.bin": files["a.bin"][0]}) as server:
        listing(box, server, files)
        done = run_bash(script, box)
        assert done.returncode == 0, done.stderr
        assert "after:unset" in done.stdout
        assert (data / "a.bin").read_bytes() == files["a.bin"][0]


def test_a_hash_mismatch_fails_the_run_and_names_the_reason(box):
    _tmp, data, state = box
    files = {"bad.bin": (payload("bad", 30_000), "weight")}
    with FileServer({"bad.bin": files["bad.bin"][0]}, corrupt="bad.bin") as server:
        listing(box, server, files)
        done = run_bash("ablit_download_all", box)
    assert done.returncode == 6
    assert failed(state)["reason"] == "hash_mismatch"
    assert not (data / "bad.bin").exists() and not (data / "bad.bin.part").exists()


def test_a_missing_file_fails_at_once_with_the_http_status(box):
    _tmp, _data, state = box
    files = {"gone.bin": (payload("gone", 10_000), "weight")}
    with FileServer({}) as server:
        listing(box, server, files)
        done = run_bash("ablit_download_all", box)
    assert done.returncode == 6
    info = failed(state)
    assert info["reason"] == "download_failed" and "404" in info["detail"]


def test_download_one_checks_size_and_hash_before_the_rename(box):
    _tmp, data, state = box
    blob = payload("one", 25_000)
    with FileServer({"one.bin": blob}) as server:
        url = server.url("one.bin")
        good = run_bash(f'ablit_download_one "{url}" sub/one.bin {len(blob)} {sha(blob)}', box)
        assert good.returncode == 0, good.stderr
        assert (data / "sub" / "one.bin").read_bytes() == blob
        bad = run_bash(f'ablit_download_one "{url}" two.bin {len(blob)} {"0" * 64}', box)
        assert bad.returncode == 6 and failed(state)["reason"] == "hash_mismatch"
        assert not (data / "two.bin").exists()


def test_consume_removes_the_file_and_remembers_it(box):
    _tmp, data, state = box
    (data / "sub").mkdir()
    (data / "sub" / "part").write_bytes(b"x")
    done = run_bash("ablit_consume sub/part", box)
    assert done.returncode == 0, done.stderr
    assert not (data / "sub" / "part").exists()
    assert (state / "consumed" / "sub%2Fpart").exists()


def test_a_failed_download_preserves_its_reason_with_an_inherited_error_trap(box):
    _tmp, _data, state = box
    blob = payload("bad-hash", 20_000)
    with FileServer({"one.bin": blob}) as server:
        script = ('set -E\ntrap \'ablit_fail command_failed "generic failure"\' ERR\n'
                  f'ablit_download_one "{server.url("one.bin")}" one.bin {len(blob)} {"0" * 64}')
        done = run_bash(script, box)
    assert done.returncode == 6
    assert failed(state)["reason"] == "hash_mismatch"


def test_role_helpers_list_names_and_sizes(box):
    tmp, _data, _state = box
    (tmp / "artifacts.tsv").write_text(
        "u1\tb/part-2\t20\t" + "0" * 64 + "\tweight\n"
        "u2\tb/part-1\t10\t" + "0" * 64 + "\tweight\n"
        "u3\ttok/a.json\t5\t" + "0" * 64 + "\ttokenizer\n")
    done = run_bash('ablit_rels_by_role weight; ablit_bytes_by_role weight; ablit_bytes_by_role missing', box)
    assert done.stdout.split() == ["b/part-1", "b/part-2", "30", "0"]


# --- the speed check ---------------------------------------------------------


def test_speed_check_passes_and_keeps_what_it_downloaded(box):
    _tmp, data, state = box
    files = {"w.bin": (payload("w", 60_000), "weight")}
    with FileServer({"w.bin": files["w.bin"][0]}) as server:
        listing(box, server, files)
        done = run_bash("ablit_speed_check weight", box,
                        {"ABLIT_MIN_DOWNLOAD_MBPS": "0.001", "ABLIT_SPEED_TEST_SECONDS": "20"})
        assert done.returncode == 0, done.stderr
        result = json.loads((state / "speed.json").read_text())
        assert result["measured_mbps"] > 0 and result["floor_mbps"] == 0.001
        assert (data / "w.bin").read_bytes() == files["w.bin"][0]


def test_speed_check_below_the_floor_fails_with_its_reason(box):
    _tmp, _data, state = box
    files = {"w.bin": (payload("w", 60_000), "weight")}
    with FileServer({"w.bin": files["w.bin"][0]}, delay_per_block=0.001) as server:
        listing(box, server, files)
        done = run_bash("ablit_speed_check weight", box,
                        {"ABLIT_MIN_DOWNLOAD_MBPS": "100000000", "ABLIT_SPEED_TEST_SECONDS": "20"})
    assert done.returncode == 6
    assert failed(state)["reason"] == "speed_below_floor"


def test_speed_check_with_nothing_left_to_download_has_no_speed_to_measure(box):
    _tmp, _data, state = box
    files = {"w.bin": (payload("w", 20_000), "weight")}
    with FileServer({"w.bin": files["w.bin"][0]}) as server:
        listing(box, server, files)
        assert run_bash("ablit_download_all", box).returncode == 0
        done = run_bash("ablit_speed_check", box, {"ABLIT_MIN_DOWNLOAD_MBPS": "100000000"})
    assert done.returncode == 0, done.stderr
    assert not (state / "failed.json").exists()


# --- background steps --------------------------------------------------------


def test_a_failed_background_step_ends_the_wait_and_stops_the_other_steps(box):
    tmp, _data, state = box
    pidfile = tmp / "sleeper.pid"
    script = f'''trap ablit_kill_jobs EXIT
slow() {{ sleep 60 & echo $! > "{pidfile}"; wait; }}
failing() {{ sleep 1; ablit_fail boom "invented failure"; }}
ablit_spawn slow slow
ablit_spawn failing failing
ablit_wait_jobs failing
echo not-reached
'''
    done = run_bash(script, box)
    assert done.returncode == 6 and "not-reached" not in done.stdout
    assert failed(state)["reason"] == "boom"
    sleeper = int(pidfile.read_text())
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            os.kill(sleeper, 0)
            # A process that exited but was not collected yet still answers, and it is not running.
            if "State:\tZ" in Path(f"/proc/{sleeper}/status").read_text():
                return
        except (ProcessLookupError, FileNotFoundError):
            return
        time.sleep(0.1)
    os.kill(sleeper, signal.SIGKILL)
    pytest.fail("the background step left its child running")


def test_waiting_for_named_steps_returns_when_they_end_and_ignores_the_rest(box):
    script = '''trap ablit_kill_jobs EXIT
quick() { sleep 0.2; }
long() { sleep 30; }
ablit_spawn quick quick
ablit_spawn long long
ablit_wait_jobs quick
echo quick-done
'''
    done = run_bash(script, box)
    assert done.returncode == 0 and "quick-done" in done.stdout


def test_a_step_that_ends_with_an_error_and_no_reason_still_fails_the_run(box):
    _tmp, _data, state = box
    script = '''trap ablit_kill_jobs EXIT
broken() { exit 3; }
ablit_spawn broken broken
ablit_wait_jobs
'''
    done = run_bash(script, box)
    assert done.returncode == 6
    assert failed(state)["reason"] == "background_step_failed"


def test_waiting_for_files_stops_when_the_run_has_failed(box):
    _tmp, data, state = box
    (state / "failed.json").write_text('{"reason":"x","detail":"","at":1}')
    done = run_bash(f'ablit_wait_for_files "{data}/never"', box, timeout=20)
    assert done.returncode == 6


def test_join_parts_appends_in_order_as_each_part_appears(box):
    _tmp, data, state = box
    script = f'''(sleep 1; printf BBB > "{data}/p2"; sleep 1; printf CCC > "{data}/p3") &
printf AAA > "{data}/p1"
ablit_join_parts out/all.bin 9 1 p1 p2 p3
wait
'''
    done = run_bash(script, box)
    assert done.returncode == 0, done.stderr
    assert (data / "out" / "all.bin").read_bytes() == b"AAABBBCCC"
    assert not any((data / name).exists() for name in ("p1", "p2", "p3"))
    assert (state / "consumed" / "p2").exists()


# --- hardware checks ---------------------------------------------------------


def stub_smi(tmp: Path, compute_cap: str, driver: str) -> dict[str, str]:
    bin_dir = tmp / "bin"
    bin_dir.mkdir()
    smi = bin_dir / "nvidia-smi"
    smi.write_text(f'''#!/bin/sh
case "$*" in
  *driver_version*) echo "{driver}" ;;
  *) echo "24576, {compute_cap}" ;;
esac
''')
    smi.chmod(0o755)
    return {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}


HARDWARE = {"ABLIT_GPU_COUNT": "1", "ABLIT_MIN_VRAM_MB_EACH": "20000", "ABLIT_MIN_HOST_RAM_GB": "1",
            "ABLIT_MIN_DISK_GB": "1"}


def test_a_gpu_generation_outside_the_image_fails_the_hardware_check(box):
    tmp, _data, state = box
    env = {**HARDWARE, **stub_smi(tmp, "7.5", "580.65"), "ABLIT_CUDA_ARCHITECTURES": "86 89 120"}
    done = run_bash("ablit_check_hardware", box, env)
    assert done.returncode == 6
    info = failed(state)
    assert info["reason"] == "gpu_generation" and "7.5" in info["detail"]


def test_an_old_driver_fails_the_hardware_check(box):
    tmp, _data, state = box
    env = {**HARDWARE, **stub_smi(tmp, "8.6", "570.124.06"), "ABLIT_MIN_DRIVER_MAJOR": "580"}
    done = run_bash("ablit_check_hardware", box, env)
    assert done.returncode == 6
    info = failed(state)
    assert info["reason"] == "driver_too_old" and "570.124.06" in info["detail"]


def test_a_listed_generation_and_a_new_driver_pass_those_two_checks(box):
    tmp, _data, state = box
    env = {**HARDWARE, **stub_smi(tmp, "8.9", "580.65"), "ABLIT_CUDA_ARCHITECTURES": "86 89 120",
           "ABLIT_MIN_DRIVER_MAJOR": "580"}
    run_bash("ablit_check_hardware", box, env)
    # The later checks read Linux files and may fail on another system. These two must not.
    if (state / "failed.json").exists():
        assert failed(state)["reason"] not in ("gpu_generation", "driver_too_old", "driver_version")


def test_arch_listed_matches_the_compute_capability_times_ten(box):
    script = '''ABLIT_CUDA_ARCHITECTURES="86 89 120"
for cc in 8.6 8.9 12.0 7.5 9.0; do
  if ablit_arch_listed "$cc"; then echo "$cc yes"; else echo "$cc no"; fi
done'''
    done = run_bash(script, box)
    assert done.stdout.split("\n")[:-1] == ["8.6 yes", "8.9 yes", "12.0 yes", "7.5 no", "9.0 no"]


def test_pid_alive_tells_a_live_a_gone_and_a_made_up_process_apart(box):
    live = subprocess.Popen(["sleep", "30"])
    gone = subprocess.Popen(["true"])
    gone.wait()
    try:
        script = f'''for pid in {live.pid} {gone.pid} "" abc 0; do
  if ablit_pid_alive "$pid"; then echo "$pid alive"; else echo "$pid dead"; fi
done'''
        done = run_bash(script, box)
    finally:
        live.kill()
        live.wait()
    assert done.stdout.split("\n")[:-1] == [f"{live.pid} alive", f"{gone.pid} dead", " dead", "abc dead", "0 dead"]


@pytest.mark.skipif(not Path("/proc/self/status").exists(), reason="the process state is read from /proc")
def test_pid_alive_says_no_for_a_process_that_exited_but_was_not_collected(box):
    # The parent below never waits for its child, so the child stays a zombie that still answers kill -0.
    parent = subprocess.Popen(
        ["python3", "-I", "-c",
         "import subprocess, sys, time\n"
         "child = subprocess.Popen(['true'])\n"
         "print(child.pid, flush=True)\n"
         "time.sleep(20)\n"],
        stdout=subprocess.PIPE, text=True)
    try:
        pid = int(parent.stdout.readline())
        for _ in range(100):
            text = (Path("/proc") / str(pid) / "status").read_text()
            if "State:\tZ" in text:
                break
            time.sleep(0.05)
        assert "State:\tZ" in text, "the child did not become a zombie"
        script = f'kill -0 {pid} && echo "kill answers"; if ablit_pid_alive {pid}; then echo alive; else echo dead; fi'
        done = run_bash(script, box)
    finally:
        parent.kill()
        parent.wait()
    assert done.stdout.split("\n")[:-1] == ["kill answers", "dead"]
