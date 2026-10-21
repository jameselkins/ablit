"""Exercise engines/lib/fetch.py against a local server that serves synthetic files.

The server speaks HTTP range requests and can be told to misbehave, so the tests
cover resume, retry, a wrong hash and a server that answers 200 to a range
request. Nothing here touches the network beyond 127.0.0.1.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time
from typing import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def payload(name: str, size: int) -> bytes:
    """Synthetic bytes that differ per file and per position."""
    seed = hashlib.sha256(name.encode()).digest()
    block = (seed * (4096 // len(seed) + 1))[:4096]
    out = bytearray()
    counter = 0
    while len(out) < size:
        out += block[: 4096 - 8] + counter.to_bytes(8, "little")
        counter += 1
    return bytes(out[:size])


class FileServer:
    """A threaded HTTP server for a dict of path to bytes."""

    def __init__(self, files: dict[str, bytes], *, honor_range: bool = True, fail_first: int = 0,
                 status_on_fail: int = 503, cut_first_body: bool = False, always_fail: bool = False,
                 delay_per_block: float = 0.0, redirect: bool = False, corrupt: str | None = None,
                 gates: dict[str, Callable[[], bool]] | None = None, gate_timeout: float = 30.0) -> None:
        self.files = files
        # A gate holds back every response for one file until its function returns True.
        # The result of each gate is kept, so a test can prove that something else
        # happened while the file was still waiting.
        self.gates = gates or {}
        self.gate_timeout = gate_timeout
        self.gate_results: dict[str, bool] = {}
        self.honor_range = honor_range
        self.fail_first = fail_first
        self.status_on_fail = status_on_fail
        self.cut_first_body = cut_first_body
        self.always_fail = always_fail
        self.delay_per_block = delay_per_block
        self.redirect = redirect
        self.corrupt = corrupt
        self.requests: list[tuple[float, str, str | None]] = []
        self.lock = threading.Lock()
        self.failed = 0
        self.cut = False
        server = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):  # keep the test output quiet
                pass

            def do_GET(self):
                path = self.path
                with server.lock:
                    server.requests.append((time.monotonic(), path, self.headers.get("Range")))
                if server.redirect and path.startswith("/r/"):
                    self.send_response(302)
                    self.send_header("Location", "/f/" + path[3:])
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                name = path.split("/f/", 1)[-1] if "/f/" in path else path.lstrip("/")
                data = server.files.get(name)
                if data is None:
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                with server.lock:
                    fail = server.always_fail or server.failed < server.fail_first
                    if fail and not server.always_fail:
                        server.failed += 1
                if fail:
                    self.send_response(server.status_on_fail)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                gate = server.gates.get(name)
                if gate is not None:
                    deadline = time.monotonic() + server.gate_timeout
                    opened = False
                    while time.monotonic() < deadline:
                        if gate():
                            opened = True
                            break
                        time.sleep(0.05)
                    with server.lock:
                        server.gate_results[name] = server.gate_results.get(name, True) and opened
                if server.corrupt == name:
                    data = bytes([data[0] ^ 1]) + data[1:]
                header = self.headers.get("Range")
                match = re.fullmatch(r"bytes=(\d+)-(\d+)", header or "")
                if match and server.honor_range:
                    first, last = int(match.group(1)), min(int(match.group(2)), len(data) - 1)
                    if first >= len(data):
                        self.send_response(416)
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return
                    body = data[first : last + 1]
                    self.send_response(206)
                    self.send_header("Content-Range", f"bytes {first}-{last}/{len(data)}")
                else:
                    body = data
                    self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                cut_here = False
                with server.lock:
                    if server.cut_first_body and not server.cut and len(body) > 100:
                        server.cut = cut_here = True
                step = 4096
                for start in range(0, len(body), step):
                    if cut_here and start >= len(body) // 3:
                        self.close_connection = True
                        return  # the connection ends early, in the middle of the body
                    try:
                        self.wfile.write(body[start : start + step])
                    except OSError:
                        return
                    if server.delay_per_block:
                        time.sleep(server.delay_per_block)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()

    def url(self, name: str, prefix: str = "f") -> str:
        return f"http://127.0.0.1:{self.httpd.server_address[1]}/{prefix}/{name}"

    def ranges_for(self, name: str) -> list[str | None]:
        return [r for (_t, p, r) in self.requests if p.endswith("/" + name)]


FETCH = Path(__file__).resolve().parents[2] / "engines" / "lib" / "fetch.py"
SMALL = {
    "ABLIT_DOWNLOAD_SPLIT_BYTES": "20000",
    "ABLIT_DOWNLOAD_CHUNK_BYTES": "16384",
    "ABLIT_DOWNLOAD_BACKOFF_SECONDS": "0.01",
    "ABLIT_DOWNLOAD_PROGRESS_SECONDS": "0.1",
}


def run_fetch(args: list[str], env: dict[str, str] | None = None, timeout: int = 60):
    full = {**os.environ, **SMALL, **(env or {})}
    done = subprocess.run([sys.executable, str(FETCH), *args], env=full, capture_output=True, text=True, timeout=timeout)
    lines = [line for line in done.stdout.splitlines() if line]
    return done.returncode, (lines[-1].split("\t") if lines else []), done.stderr


def write_list(path: Path, server: FileServer, files: dict[str, tuple[bytes, str]], prefix: str = "f") -> None:
    rows = []
    for name, (data, role) in files.items():
        rows.append(f"{server.url(name, prefix)}\t{name}\t{len(data)}\t{sha(data)}\t{role}\n")
    path.write_text("".join(rows))


@pytest.fixture
def dirs(tmp_path):
    data, state = tmp_path / "data", tmp_path / "state"
    data.mkdir()
    state.mkdir()
    return data, state


def all_args(tsv: Path, data: Path, state: Path, *extra: str) -> list[str]:
    return ["all", "--tsv", str(tsv), "--root", str(data), "--state", str(state), *extra]


def test_several_files_download_in_ranges_and_are_verified(dirs, tmp_path):
    data, state = dirs
    files = {"big.bin": payload("big", 100_000), "mid.bin": payload("mid", 50_000), "tiny.txt": b"hello\n"}
    with FileServer(files) as server:
        tsv = tmp_path / "list.tsv"
        write_list(tsv, server, {k: (v, "weight") for k, v in files.items()})
        code, result, _err = run_fetch(all_args(tsv, data, state))
        assert code == 0 and result[0] == "OK" and int(result[1]) == sum(map(len, files.values()))
        for name, content in files.items():
            assert (data / name).read_bytes() == content
            assert not (data / (name + ".part")).exists() and not (data / (name + ".part.state")).exists()
        # The big file was cut into several ranges, and the tiny file was fetched whole.
        big_ranges = server.ranges_for("big.bin")
        assert len(big_ranges) == 7 and all(r and r.startswith("bytes=") for r in big_ranges)
        assert server.ranges_for("tiny.txt") == ["bytes=0-5"]
    progress = json.loads((state / "progress" / "download.json").read_text())
    assert progress["bytes_done"] == progress["bytes_total"] == sum(map(len, files.values()))
    assert progress["files_done"] == progress["files_total"] == 3
    marker = json.loads((state / "verified" / "big.bin.json").read_text())
    assert marker["sha256"] == sha(files["big.bin"]) and marker["bytes"] == 100_000


def test_a_second_run_downloads_nothing_and_does_not_hash_again(dirs, tmp_path):
    data, state = dirs
    files = {"a.bin": payload("a", 40_000)}
    with FileServer(files) as server:
        tsv = tmp_path / "list.tsv"
        write_list(tsv, server, {"a.bin": (files["a.bin"], "weight")})
        assert run_fetch(all_args(tsv, data, state))[0] == 0
        before = len(server.requests)
        code, result, _ = run_fetch(all_args(tsv, data, state))
        assert code == 0 and result[:2] == ["OK", "0"]
        assert len(server.requests) == before


def test_a_consumed_file_is_not_downloaded_again(dirs, tmp_path):
    data, state = dirs
    files = {"part-1": payload("p1", 30_000)}
    with FileServer(files) as server:
        tsv = tmp_path / "list.tsv"
        write_list(tsv, server, {"part-1": (files["part-1"], "weight")})
        assert run_fetch(all_args(tsv, data, state))[0] == 0
        (data / "part-1").unlink()
        (state / "consumed").mkdir()
        (state / "consumed" / "part-1").write_text("")
        count = len(server.requests)
        assert run_fetch(all_args(tsv, data, state))[1][0] == "OK"
        assert len(server.requests) == count and not (data / "part-1").exists()


def test_resume_continues_each_chunk_where_it_stopped(dirs, tmp_path):
    data, state = dirs
    content = payload("resume", 65_536)
    name = "resume.bin"
    # Chunks of 16384 bytes. The first is complete and the second is half done.
    part = data / (name + ".part")
    body = bytearray(len(content))
    body[:16384 + 8192] = content[:16384 + 8192]
    part.write_bytes(bytes(body))
    chunks = [[0, 16384, 16384], [16384, 32768, 8192], [32768, 49152, 0], [49152, 65536, 0]]
    (data / (name + ".part.state")).write_text(
        json.dumps({"v": 1, "size": len(content), "sha256": sha(content), "chunks": chunks}))
    with FileServer({name: content}) as server:
        tsv = tmp_path / "list.tsv"
        write_list(tsv, server, {name: (content, "weight")})
        code, result, _ = run_fetch(all_args(tsv, data, state))
        assert code == 0 and result[0] == "OK"
        starts = sorted(int(r.split("=")[1].split("-")[0]) for r in server.ranges_for(name))
        assert starts == [16384 + 8192, 32768, 49152]
        assert int(result[1]) == 65_536 - 16384 - 8192
    assert (data / name).read_bytes() == content


def test_a_plain_partial_file_from_an_older_download_is_continued(dirs, tmp_path):
    data, state = dirs
    content = payload("legacy", 50_000)
    (data / "legacy.bin.part").write_bytes(content[:20_000])
    with FileServer({"legacy.bin": content}) as server:
        tsv = tmp_path / "list.tsv"
        write_list(tsv, server, {"legacy.bin": (content, "weight")})
        assert run_fetch(all_args(tsv, data, state))[0] == 0
        assert min(int(r.split("=")[1].split("-")[0]) for r in server.ranges_for("legacy.bin")) == 20_000
    assert (data / "legacy.bin").read_bytes() == content


def test_a_complete_part_file_is_verified_and_renamed_without_a_request(dirs, tmp_path):
    data, state = dirs
    content = payload("done", 30_000)
    (data / "done.bin.part").write_bytes(content)
    with FileServer({"done.bin": content}) as server:
        tsv = tmp_path / "list.tsv"
        write_list(tsv, server, {"done.bin": (content, "weight")})
        assert run_fetch(all_args(tsv, data, state))[0] == 0
        assert server.requests == []
    assert (data / "done.bin").read_bytes() == content


def test_failed_attempts_are_retried_and_a_cut_connection_continues(dirs, tmp_path):
    data, state = dirs
    content = payload("flaky", 12_000)
    with FileServer({"flaky.bin": content}, fail_first=2, cut_first_body=True) as server:
        tsv = tmp_path / "list.tsv"
        write_list(tsv, server, {"flaky.bin": (content, "weight")})
        code, result, err = run_fetch(all_args(tsv, data, state))
        assert code == 0 and result[0] == "OK", err
        assert "attempt" in err
    assert (data / "flaky.bin").read_bytes() == content


def test_a_server_that_keeps_failing_ends_the_run_with_a_reason(dirs, tmp_path):
    data, state = dirs
    content = payload("down", 12_000)
    with FileServer({"down.bin": content}, always_fail=True) as server:
        tsv = tmp_path / "list.tsv"
        write_list(tsv, server, {"down.bin": (content, "weight")})
        code, result, _ = run_fetch(all_args(tsv, data, state), {"ABLIT_DOWNLOAD_RETRIES": "2"})
    assert code == 4 and result[:2] == ["FAIL", "download_failed"]
    assert not (data / "down.bin").exists()


def test_a_missing_file_fails_at_once_without_retries(dirs, tmp_path):
    data, state = dirs
    content = payload("gone", 5000)
    with FileServer({}) as server:
        tsv = tmp_path / "list.tsv"
        write_list(tsv, server, {"gone.bin": (content, "weight")})
        code, result, _ = run_fetch(all_args(tsv, data, state))
        assert code == 4 and result[:2] == ["FAIL", "download_failed"] and "404" in result[2]
        assert len(server.requests) == 1


def test_wrong_bytes_fail_the_hash_and_remove_the_part_file(dirs, tmp_path):
    data, state = dirs
    content = payload("bad", 60_000)
    with FileServer({"bad.bin": content}, corrupt="bad.bin") as server:
        tsv = tmp_path / "list.tsv"
        write_list(tsv, server, {"bad.bin": (content, "weight")})
        code, result, _ = run_fetch(all_args(tsv, data, state))
    assert code == 4 and result[:3] == ["FAIL", "hash_mismatch", "bad.bin"]
    assert not (data / "bad.bin").exists() and not (data / "bad.bin.part").exists()
    assert not (data / "bad.bin.part.state").exists()


def test_a_file_of_the_wrong_size_fails_with_size_mismatch(dirs, tmp_path):
    data, state = dirs
    content = payload("short", 60_000)
    with FileServer({"short.bin": content[:50_000]}) as server:
        tsv = tmp_path / "list.tsv"
        write_list(tsv, server, {"short.bin": (content, "weight")})
        code, result, _ = run_fetch(all_args(tsv, data, state))
    assert code == 4 and result[:2] == ["FAIL", "size_mismatch"]
    assert not (data / "short.bin").exists()


def test_a_200_answer_to_a_range_request_fails_the_run(dirs, tmp_path):
    data, state = dirs
    content = payload("norange", 60_000)
    with FileServer({"norange.bin": content}, honor_range=False) as server:
        tsv = tmp_path / "list.tsv"
        write_list(tsv, server, {"norange.bin": (content, "weight")})
        code, result, _ = run_fetch(all_args(tsv, data, state))
    assert code == 4 and result[:2] == ["FAIL", "range_not_honored"]
    assert not (data / "norange.bin").exists()


def test_a_200_answer_is_accepted_when_the_whole_file_was_asked_for(dirs, tmp_path):
    data, state = dirs
    content = payload("whole", 5000)
    with FileServer({"whole.bin": content}, honor_range=False) as server:
        tsv = tmp_path / "list.tsv"
        write_list(tsv, server, {"whole.bin": (content, "tokenizer")})
        assert run_fetch(all_args(tsv, data, state))[0] == 0
    assert (data / "whole.bin").read_bytes() == content


def test_a_200_answer_to_a_resumed_request_fails_the_run(dirs, tmp_path):
    data, state = dirs
    content = payload("resumed", 5000)
    (data / "resumed.bin.part").write_bytes(content[:2000])
    with FileServer({"resumed.bin": content}, honor_range=False) as server:
        tsv = tmp_path / "list.tsv"
        write_list(tsv, server, {"resumed.bin": (content, "tokenizer")})
        code, result, _ = run_fetch(all_args(tsv, data, state))
    assert code == 4 and result[:2] == ["FAIL", "range_not_honored"]


def test_redirects_are_followed_for_every_range(dirs, tmp_path):
    data, state = dirs
    content = payload("redir", 50_000)
    with FileServer({"redir.bin": content}, redirect=True) as server:
        tsv = tmp_path / "list.tsv"
        write_list(tsv, server, {"redir.bin": (content, "weight")}, prefix="r")
        assert run_fetch(all_args(tsv, data, state))[0] == 0
        assert len([p for (_t, p, _r) in server.requests if p.startswith("/r/")]) == 4
    assert (data / "redir.bin").read_bytes() == content


def test_files_start_in_list_order_and_roles_choose_the_files(dirs, tmp_path):
    data, state = dirs
    files = {f"f{n}.bin": payload(f"f{n}", 30_000) for n in range(4)}
    roles = {"f0.bin": "weight", "f1.bin": "engram-source", "f2.bin": "weight", "f3.bin": "engram-source"}
    with FileServer(files) as server:
        tsv = tmp_path / "list.tsv"
        write_list(tsv, server, {k: (v, roles[k]) for k, v in files.items()})
        one_at_a_time = {"ABLIT_DOWNLOAD_FILES": "1", "ABLIT_DOWNLOAD_RANGES": "1", "ABLIT_DOWNLOAD_CONNECTIONS": "1"}
        code, result, _ = run_fetch(all_args(tsv, data, state, "--roles", "engram-source"), one_at_a_time)
        assert code == 0 and not (data / "f0.bin").exists() and (data / "f1.bin").exists()
        order = [p.rsplit("/", 1)[1] for (_t, p, _r) in server.requests]
        assert order[:2] == ["f1.bin", "f1.bin"] and set(order) == {"f1.bin", "f3.bin"}
        assert order.index("f3.bin") > order.index("f1.bin")
        # Progress counts the whole list, so a later pass over everything keeps rising.
        progress = json.loads((state / "progress" / "download.json").read_text())
        assert progress["bytes_total"] == 120_000 and progress["bytes_done"] == 60_000
        code, result, _ = run_fetch(all_args(tsv, data, state), one_at_a_time)
        assert code == 0 and (data / "f0.bin").exists() and (data / "f2.bin").exists()


def test_the_time_limit_stops_the_run_and_the_next_run_finishes(dirs, tmp_path):
    data, state = dirs
    content = payload("slow", 200_000)
    with FileServer({"slow.bin": content}, delay_per_block=0.05) as server:
        tsv = tmp_path / "list.tsv"
        write_list(tsv, server, {"slow.bin": (content, "weight")})
        env = {"ABLIT_DOWNLOAD_RANGES": "2", "ABLIT_DOWNLOAD_CONNECTIONS": "2"}
        code, result, _ = run_fetch(all_args(tsv, data, state, "--time-limit", "0.4"), env)
        assert code == 3 and result[0] == "LIMIT"
        first = int(result[1])
        assert 0 < first < len(content) and not (data / "slow.bin").exists()
        code, result, _ = run_fetch(all_args(tsv, data, state), env)
        assert code == 0 and result[0] == "OK"
        assert first + int(result[1]) < len(content) * 1.3
    assert (data / "slow.bin").read_bytes() == content


def test_the_hash_is_computed_while_the_download_runs(dirs, tmp_path):
    """The last byte arrives and the file is renamed a moment later, with no second pass over the file."""
    data, state = dirs
    content = payload("follow", 300_000)
    with FileServer({"follow.bin": content}, delay_per_block=0.002) as server:
        tsv = tmp_path / "list.tsv"
        write_list(tsv, server, {"follow.bin": (content, "weight")})
        started = time.monotonic()
        code, result, err = run_fetch(all_args(tsv, data, state))
        assert code == 0 and "verified follow.bin" in err
    assert (data / "follow.bin").read_bytes() == content
    assert time.monotonic() - started < 30


def test_one_downloads_a_single_file(dirs):
    data, state = dirs
    content = payload("single", 45_000)
    with FileServer({"single.bin": content}) as server:
        code, result, _ = run_fetch(["one", "--url", server.url("single.bin"), "--rel", "sub/single.bin",
                                     "--bytes", str(len(content)), "--sha256", sha(content),
                                     "--root", str(data), "--state", str(state)])
    assert code == 0 and result[0] == "OK"
    assert (data / "sub" / "single.bin").read_bytes() == content


def test_a_bad_list_line_is_refused(dirs, tmp_path):
    data, state = dirs
    tsv = tmp_path / "list.tsv"
    tsv.write_text("http://127.0.0.1:1/x\t../escape\t5\t" + "0" * 64 + "\tweight\n")
    code, result, _ = run_fetch(all_args(tsv, data, state))
    assert code == 4 and result[1] == "bad_artifact_list"


def test_join_appends_parts_as_they_appear_and_removes_them(dirs):
    data, state = dirs
    parts = {f"p{n}": payload(f"p{n}", 20_000 + n) for n in range(3)}
    for name in ("p0",):
        (data / name).write_bytes(parts[name])

    def late():
        for name in ("p1", "p2"):
            time.sleep(0.6)
            (data / (name + ".tmp")).write_bytes(parts[name])
            os.replace(data / (name + ".tmp"), data / name)

    thread = threading.Thread(target=late)
    thread.start()
    total = sum(map(len, parts.values()))
    code, result, _ = run_fetch(["join", "--root", str(data), "--state", str(state), "--out", "joined/model.gguf",
                                 "--expect-bytes", str(total), "--delete-inputs", "p0", "p1", "p2"])
    thread.join()
    assert code == 0 and result[:2] == ["OK", str(total)]
    assert (data / "joined" / "model.gguf").read_bytes() == b"".join(parts.values())
    assert not any((data / name).exists() for name in parts)
    assert all((state / "consumed" / name).exists() for name in parts)
    progress = json.loads((state / "progress" / "join.json").read_text())
    assert progress["bytes_done"] == progress["bytes_total"] == total


def test_join_resumes_after_a_crash_and_checks_the_final_size(dirs):
    data, state = dirs
    parts = {f"p{n}": payload(f"p{n}", 10_000) for n in range(3)}
    for name, content in parts.items():
        (data / name).write_bytes(content)
    # An earlier run appended the first part and then died while it wrote the second.
    out = data / "model.gguf"
    out.write_bytes(parts["p0"] + b"junk from a half written part")
    (state / "steps").mkdir()
    (state / "steps" / "join.json").write_text(json.dumps({"out": "model.gguf", "appended": 1, "offset": 10_000}))
    code, result, _ = run_fetch(["join", "--root", str(data), "--state", str(state), "--out", "model.gguf",
                                 "--expect-bytes", "30000", "p0", "p1", "p2"])
    assert code == 0 and out.read_bytes() == b"".join(parts.values())
    assert all((data / name).exists() for name in parts)  # no --delete-inputs
    code, result, _ = run_fetch(["join", "--root", str(data), "--state", str(state), "--out", "model2.gguf",
                                 "--expect-bytes", "29999", "p0", "p1", "p2"])
    assert code == 4 and result[1] == "size_mismatch"


def test_join_gives_up_when_the_run_has_already_failed(dirs):
    data, state = dirs
    (state / "failed.json").write_text("{}")
    code, result, _ = run_fetch(["join", "--root", str(data), "--state", str(state), "--out", "m.gguf",
                                 "--expect-bytes", "5", "missing-part"])
    assert code == 4 and result[1] == "join_stopped"


def test_watch_reports_growth_until_the_process_ends(dirs):
    data, state = dirs
    (data / "x").write_bytes(b"a" * 1000)
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(0.8)"])
    threading.Thread(target=child.wait, daemon=True).start()  # reap it, so it is not a zombie
    code, result, _ = run_fetch(["watch", "--state", str(state), "--label", "extract", "--total", "4000",
                                 "--pid", str(child.pid), "--interval", "0.1", str(data)])
    child.wait()
    assert code == 0
    progress = json.loads((state / "progress" / "extract.json").read_text())
    assert progress["bytes_done"] == 1000 and progress["bytes_total"] == 4000
