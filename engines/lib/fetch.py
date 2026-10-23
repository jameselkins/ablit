#!/usr/bin/env python3
"""Fast, resumable, hash-checked file transfer for the rented machine.

The engine bootstrap scripts call this file through the functions in
common.sh. It uses only the Python standard library, so the llama.cpp image
and the Xolotl image can both run it.

What it does:

1. It downloads several files at once. Each large file is cut into chunks, and
   many chunks of one file are fetched in parallel with HTTP range requests.
   Chunks are handed out in list order, so the first files in the list finish
   first.
2. It writes every chunk at its own offset in a "<name>.part" file, and it saves
   how far each chunk got in "<name>.part.state". A restart continues each
   chunk where it stopped.
3. A hashing thread per file reads the finished prefix of the file while the
   download goes on, so the SHA-256 is ready a moment after the last byte
   arrives. There is no second full read.
4. A file is renamed to its final name only after its size and SHA-256 match
   the list. A mismatch deletes the part file and fails the run.
5. It writes progress for the CLI and a verified marker for every finished file.

Sub-commands:

  all    download the files of an artifact list (tab separated, see common.sh)
  one    download a single file
  join   append finished parts to one output file, in order, as they appear
  watch  report the growth of files while another process writes them

Every sub-command prints exactly one result line on standard output.

  OK <TAB> new_bytes <TAB> seconds <TAB> recent_mbps
  LIMIT <TAB> new_bytes <TAB> seconds <TAB> recent_mbps     (the time limit ended the run)
  FAIL <TAB> reason <TAB> detail

Exit codes are 0 for OK, 3 for LIMIT, 4 for FAIL and 2 for a usage error.
Log lines go to standard error.

Settings come from environment variables, so the engine's lock file can list
them with the other variables:

  ABLIT_DOWNLOAD_FILES            files with open chunks at once (default 8)
  ABLIT_DOWNLOAD_RANGES           parallel chunks for one file (default 8)
  ABLIT_DOWNLOAD_CONNECTIONS      all connections together (default files x ranges)
  ABLIT_DOWNLOAD_SPLIT_BYTES      files below this size use one request (default 4 GiB)
  ABLIT_DOWNLOAD_CHUNK_BYTES      chunk size for bigger files (default 1 GiB)
  ABLIT_DOWNLOAD_RETRIES          failed attempts in a row before the run fails (default 6)
  ABLIT_DOWNLOAD_BACKOFF_SECONDS  first wait between attempts, doubled each time (default 2)
  ABLIT_DOWNLOAD_STALL_SECONDS    seconds without data before an attempt is dropped (default 30)

untested on a rented machine. The tests in cli/tests/test_fetch.py run it
against a local server only.
"""

from __future__ import annotations

import argparse
import errno
import hashlib
import http.client
import json
import os
import random
import ssl
import sys
import threading
import time
import urllib.parse
from typing import Dict, List, Optional, Tuple

MIB = 1 << 20
GIB = 1 << 30
READ_BYTES = MIB
HASH_BYTES = 8 * MIB
STATE_VERSION = 1


def log(message: str) -> None:
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    print(f"{stamp} {message}", file=sys.stderr, flush=True)


class Fatal(Exception):
    """A failure that ends the run. The reason is a short code for failed.json."""

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


class Retryable(Exception):
    """A failure of one attempt. The chunk tries again from where it stopped."""


class Config:
    def __init__(self, environ: Optional[Dict[str, str]] = None) -> None:
        env = os.environ if environ is None else environ

        def number(name: str, default: float) -> float:
            raw = env.get(name, "")
            try:
                return float(raw) if raw != "" else default
            except ValueError:
                return default

        self.files = max(1, int(number("ABLIT_DOWNLOAD_FILES", 8)))
        self.ranges = max(1, int(number("ABLIT_DOWNLOAD_RANGES", 8)))
        self.connections = max(1, int(number("ABLIT_DOWNLOAD_CONNECTIONS", self.files * self.ranges)))
        self.split_bytes = max(1, int(number("ABLIT_DOWNLOAD_SPLIT_BYTES", 4 * GIB)))
        self.chunk_bytes = max(1, int(number("ABLIT_DOWNLOAD_CHUNK_BYTES", GIB)))
        self.retries = max(0, int(number("ABLIT_DOWNLOAD_RETRIES", 6)))
        self.backoff = max(0.0, number("ABLIT_DOWNLOAD_BACKOFF_SECONDS", 2.0))
        self.stall = max(0.5, number("ABLIT_DOWNLOAD_STALL_SECONDS", 30.0))
        self.progress_every = max(0.1, number("ABLIT_DOWNLOAD_PROGRESS_SECONDS", 2.0))
        self.log_every = max(1.0, number("ABLIT_DOWNLOAD_LOG_SECONDS", 30.0))


def reserve_space(fd: int, size: int) -> None:
    """Reserve disk space for a whole file without writing it.

    This fails early on a full disk, and it lets the file system place the
    chunks that many connections write at once in a few large extents. A file
    system that cannot reserve space is skipped on purpose, because the libc
    fallback would write zeros for the whole file.
    """
    try:
        import ctypes

        call = ctypes.CDLL(None, use_errno=True).fallocate
        call.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int64, ctypes.c_int64]
        call.restype = ctypes.c_int
    except (OSError, AttributeError, ImportError):
        return
    if call(fd, 0, 0, size) != 0 and ctypes.get_errno() == errno.ENOSPC:
        raise OSError(errno.ENOSPC, "no space left on the device")


def marker_name(rel: str) -> str:
    """A flat file name for a relative path, used by the marker directories."""
    return rel.replace("/", "%2F")


def write_json_atomic(path: str, value: object) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    temporary = f"{path}.tmp{os.getpid()}"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(value, handle)
    os.replace(temporary, path)


def read_json(path: str) -> Optional[dict]:
    try:
        with open(path, encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def failed_marker_exists(state_dir: str) -> bool:
    return os.path.exists(os.path.join(state_dir, "failed.json"))


# --- one chunk of one file ---------------------------------------------------


class Chunk:
    def __init__(self, start: int, end: int, done: int = 0) -> None:
        self.start = start
        self.end = end  # exclusive
        self.done = done
        self.assigned = False

    @property
    def length(self) -> int:
        return self.end - self.start

    @property
    def complete(self) -> bool:
        return self.done >= self.length


def plan_chunks(size: int, config: Config) -> List[Chunk]:
    if size <= 0:
        return []
    if size < config.split_bytes:
        return [Chunk(0, size)]
    chunks = []
    start = 0
    while start < size:
        end = min(size, start + config.chunk_bytes)
        chunks.append(Chunk(start, end))
        start = end
    return chunks


class FileJob:
    """One file to download and verify."""

    def __init__(self, url: str, rel: str, size: int, sha256: str, root: str, state_dir: str) -> None:
        self.url = url
        self.rel = rel
        self.size = size
        self.sha256 = sha256.lower()
        self.dest = os.path.join(root, rel)
        self.part = self.dest + ".part"
        self.state_path = self.part + ".state"
        self.verified_path = os.path.join(state_dir, "verified", marker_name(rel) + ".json")
        self.consumed_path = os.path.join(state_dir, "consumed", marker_name(rel))
        self.chunks: List[Chunk] = []
        self.inflight = 0
        self.fd: Optional[int] = None
        self.started = False
        self.done = False
        self.dirty = False
        self.cond = threading.Condition()
        self.hasher: Optional[threading.Thread] = None

    # --- progress numbers

    def bytes_done(self) -> int:
        if self.done:
            return self.size
        return sum(min(c.done, c.length) for c in self.chunks)

    def contiguous(self) -> int:
        """Bytes at the start of the file that are all on disk."""
        total = 0
        for chunk in self.chunks:
            total += min(chunk.done, chunk.length)
            if not chunk.complete:
                break
        return total

    def has_unassigned(self) -> bool:
        return not self.done and any(not c.assigned and not c.complete for c in self.chunks)

    # --- start and restart

    def already_verified(self) -> bool:
        """True when the final file is there and an earlier run verified it."""
        if os.path.exists(self.consumed_path):
            return True
        try:
            info = os.stat(self.dest)
        except OSError:
            return False
        marker = read_json(self.verified_path)
        return bool(
            marker
            and info.st_size == self.size
            and marker.get("bytes") == self.size
            and marker.get("sha256") == self.sha256
            and marker.get("mtime_ns") == info.st_mtime_ns
        )

    def restore(self, config: Config) -> bool:
        """Pick up an earlier attempt. Returns True when a part file is in use."""
        if os.path.exists(self.dest):
            # A finished file without a marker is checked again as a part file.
            try:
                if os.stat(self.dest).st_size == self.size:
                    os.replace(self.dest, self.part)
                    self.chunks = [Chunk(c.start, c.end, c.length) for c in plan_chunks(self.size, config)]
                    return self._open_part()
            except OSError:
                pass
            self._remove(self.dest)
        if not os.path.exists(self.part):
            self._remove(self.state_path)
            return False
        have = os.stat(self.part).st_size
        saved = read_json(self.state_path)
        chunks = self._chunks_from_state(saved)
        if chunks is not None and have == self.size:
            self.chunks = chunks
        elif have > self.size:
            self._remove(self.part)
            self._remove(self.state_path)
            return False
        elif saved is None:
            # A part file from a plain sequential download: the first `have` bytes are good.
            self.chunks = plan_chunks(self.size, config)
            for chunk in self.chunks:
                chunk.done = max(0, min(chunk.length, have - chunk.start))
        else:
            self._remove(self.part)
            self._remove(self.state_path)
            return False
        return self._open_part()

    def _chunks_from_state(self, saved: Optional[dict]) -> Optional[List[Chunk]]:
        if not saved or saved.get("v") != STATE_VERSION or saved.get("size") != self.size:
            return None
        if saved.get("sha256") != self.sha256:
            return None
        rows = saved.get("chunks")
        if not isinstance(rows, list):
            return None
        chunks = []
        expected = 0
        for row in rows:
            if not (isinstance(row, list) and len(row) == 3 and all(isinstance(x, int) for x in row)):
                return None
            start, end, done = row
            if start != expected or end <= start or done < 0 or done > end - start:
                return None
            chunks.append(Chunk(start, end, done))
            expected = end
        return chunks if expected == self.size else None

    def plan(self, config: Config) -> None:
        """Cut a file that has no earlier attempt into chunks. Nothing is created yet."""
        self.chunks = plan_chunks(self.size, config)

    def _open_part(self) -> bool:
        self.fd = os.open(self.part, os.O_RDWR | os.O_CREAT, 0o644)
        if os.fstat(self.fd).st_size != self.size:
            try:
                reserve_space(self.fd, self.size)
            except OSError as error:
                raise Fatal("download_failed", f"{self.rel}: not enough disk space ({error.strerror})")
            if os.fstat(self.fd).st_size != self.size:
                os.ftruncate(self.fd, self.size)
        self.started = True
        self.dirty = True
        return True

    def _remove(self, path: str) -> None:
        try:
            os.unlink(path)
        except OSError:
            pass

    # --- state file

    def save_state(self) -> None:
        if not self.started or self.done:
            return
        rows = [[c.start, c.end, min(c.done, c.length)] for c in self.chunks]
        write_json_atomic(
            self.state_path,
            {"v": STATE_VERSION, "size": self.size, "sha256": self.sha256, "chunks": rows},
        )
        self.dirty = False

    # --- end of a file

    def close(self) -> None:
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = None

    def discard(self) -> None:
        self.close()
        self.started = False  # the state file must not be written again
        self._remove(self.part)
        self._remove(self.state_path)

    def finish(self) -> None:
        """Rename the verified part file and record the result."""
        self.close()
        if os.stat(self.part).st_size != self.size:
            raise Fatal("size_mismatch", self.rel)
        os.replace(self.part, self.dest)
        self._remove(self.state_path)
        info = os.stat(self.dest)
        write_json_atomic(
            self.verified_path,
            {"bytes": self.size, "sha256": self.sha256, "mtime_ns": info.st_mtime_ns},
        )
        with self.cond:
            self.done = True
            self.cond.notify_all()


# --- one run -----------------------------------------------------------------


class Transfer:
    def __init__(
        self,
        jobs: List[FileJob],
        config: Config,
        state_dir: str,
        label: str = "download",
        time_limit: float = 0.0,
        base_done: int = 0,
        base_total: int = 0,
        base_files: int = 0,
        files_total: int = 0,
    ) -> None:
        self.jobs = jobs
        self.config = config
        self.state_dir = state_dir
        self.label = label
        self.time_limit = time_limit
        self.cond = threading.Condition()
        self.stop = threading.Event()
        self.fatal: Optional[Fatal] = None
        self.new_bytes = 0
        self.bytes_lock = threading.Lock()
        self.base_done = base_done
        self.base_total = base_total
        self.base_files = base_files
        self.files_total = files_total
        self.history: List[Tuple[float, int]] = []
        self.last_mbps = 0.0
        self.started_at = 0.0

    # --- failure and counting

    def fail(self, error: Fatal) -> None:
        with self.cond:
            if self.fatal is None:
                self.fatal = error
            self.stop.set()
            self.cond.notify_all()

    def add_bytes(self, count: int) -> None:
        with self.bytes_lock:
            self.new_bytes += count

    # --- scheduling

    def next_task(self) -> Optional[Tuple[FileJob, Chunk]]:
        with self.cond:
            while not self.stop.is_set():
                waiting = [j for j in self.jobs if j.has_unassigned()]
                if not waiting:
                    return None
                for job in waiting[: self.config.files]:
                    if job.inflight >= self.config.ranges:
                        continue
                    for chunk in job.chunks:
                        if not chunk.assigned and not chunk.complete:
                            if not job.started:
                                job._open_part()
                                self._start_hasher(job)
                            chunk.assigned = True
                            job.inflight += 1
                            return job, chunk
                self.cond.wait(0.5)
            return None

    def release(self, job: FileJob, chunk: Chunk) -> None:
        with self.cond:
            job.inflight -= 1
            if not chunk.complete:
                chunk.assigned = False
            self.cond.notify_all()
        with job.cond:
            job.dirty = True
            job.cond.notify_all()

    # --- workers

    def worker(self) -> None:
        while True:
            try:
                task = self.next_task()
            except Fatal as error:
                self.fail(error)
                return
            if task is None:
                return
            job, chunk = task
            try:
                self.fetch_chunk(job, chunk)
            except Fatal as error:
                self.fail(error)
            except Exception as error:  # a bug must still stop the run with a reason
                self.fail(Fatal("download_failed", f"{job.rel}: unexpected error {type(error).__name__}: {error}"))
            finally:
                self.release(job, chunk)

    def sleep(self, seconds: float) -> None:
        self.stop.wait(seconds)

    def fetch_chunk(self, job: FileJob, chunk: Chunk) -> None:
        failures = 0
        while not self.stop.is_set() and not chunk.complete:
            before = chunk.done
            try:
                self.attempt(job, chunk)
            except Retryable as error:
                if chunk.done > before:
                    failures = 0
                failures += 1
                if failures > self.config.retries:
                    raise Fatal("download_failed", f"{job.rel}: {error}")
                wait = self.config.backoff * (2 ** (failures - 1)) * (0.5 + random.random() / 2)
                log(f"download of {job.rel} failed ({error}), attempt {failures} of {self.config.retries}")
                self.sleep(min(wait, 60.0))

    def attempt(self, job: FileJob, chunk: Chunk) -> None:
        first = chunk.start + chunk.done
        last = chunk.end - 1
        whole_file = first == 0 and last == job.size - 1
        response, connection = self.open(job, first, last)
        try:
            self.check_response(job, response, first, last, whole_file)
            buffer = bytearray(READ_BYTES)
            view = memoryview(buffer)
            offset = first
            while not self.stop.is_set() and offset <= last:
                want = min(READ_BYTES, last + 1 - offset)
                try:
                    count = response.readinto(view[:want])
                except (OSError, http.client.HTTPException) as error:
                    raise Retryable(f"{type(error).__name__} while reading") from error
                if not count:
                    raise Retryable("the connection closed early")
                self.write_at(job, view[:count], offset)
                offset += count
                chunk.done += count
                self.add_bytes(count)
        finally:
            connection.close()

    def write_at(self, job: FileJob, data: memoryview, offset: int) -> None:
        fd = job.fd
        if fd is None:
            raise Fatal("download_failed", f"{job.rel}: the part file is closed")
        written = 0
        try:
            while written < len(data):
                written += os.pwrite(fd, data[written:], offset + written)
        except OSError as error:
            raise Fatal("download_failed", f"{job.rel}: cannot write the part file ({error.strerror})")
        with job.cond:
            job.cond.notify_all()

    def open(self, job: FileJob, first: int, last: int):
        url = job.url
        for _hop in range(9):
            parts = urllib.parse.urlsplit(url)
            if parts.scheme not in ("http", "https") or not parts.hostname:
                raise Fatal("download_failed", f"{job.rel}: unsupported URL")
            if parts.scheme == "https":
                connection = http.client.HTTPSConnection(
                    parts.hostname, parts.port, timeout=self.config.stall, context=ssl.create_default_context()
                )
            else:
                connection = http.client.HTTPConnection(parts.hostname, parts.port, timeout=self.config.stall)
            target = parts.path or "/"
            if parts.query:
                target += "?" + parts.query
            try:
                connection.request(
                    "GET",
                    target,
                    headers={
                        "Range": f"bytes={first}-{last}",
                        "Accept-Encoding": "identity",
                        "User-Agent": "ablit-fetch/1",
                    },
                )
                response = connection.getresponse()
            except (OSError, http.client.HTTPException) as error:
                connection.close()
                raise Retryable(f"{type(error).__name__} while connecting") from error
            if response.status in (301, 302, 303, 307, 308):
                location = response.getheader("Location")
                connection.close()
                if not location:
                    raise Fatal("download_failed", f"{job.rel}: a redirect had no location")
                url = urllib.parse.urljoin(url, location)
                continue
            return response, connection
        raise Fatal("download_failed", f"{job.rel}: too many redirects")

    def check_response(self, job: FileJob, response, first: int, last: int, whole_file: bool) -> None:
        status = response.status
        if status == 206:
            header = response.getheader("Content-Range") or ""
            # Expected form: "bytes FIRST-LAST/TOTAL"
            try:
                span, total = header.replace("bytes", "", 1).strip().split("/")
                start_text, end_text = span.split("-")
                start, end = int(start_text), int(end_text)
            except ValueError:
                raise Fatal("range_not_honored", f"{job.rel}: the Content-Range header is missing or unreadable")
            if total != "*" and int(total) != job.size:
                raise Fatal("size_mismatch", f"{job.rel}: the server says the file has {total} bytes, the list says {job.size}")
            if start != first or end > last:
                raise Fatal("range_not_honored", f"{job.rel}: the server answered a different range")
            return
        if status == 200:
            length = response.getheader("Content-Length")
            if whole_file and length is not None and int(length) == job.size:
                return  # the whole file was asked for, and the whole file came back
            raise Fatal("range_not_honored", f"{job.rel}: the server answered 200 to a range request")
        if status == 416:
            raise Fatal("size_mismatch", f"{job.rel}: the server says the range is outside the file")
        if status in (408, 425, 429) or status >= 500:
            raise Retryable(f"HTTP {status}")
        raise Fatal("download_failed", f"{job.rel}: HTTP {status}")

    # --- hashing

    def _start_hasher(self, job: FileJob) -> None:
        thread = threading.Thread(target=self.hash_file, args=(job,), name=f"hash-{job.rel}", daemon=True)
        job.hasher = thread
        thread.start()

    def hash_file(self, job: FileJob) -> None:
        try:
            digest = hashlib.sha256()
            position = 0
            while position < job.size:
                with job.cond:
                    while job.contiguous() <= position and not self.stop.is_set():
                        job.cond.wait(0.5)
                    if self.stop.is_set():
                        return
                    available = job.contiguous()
                fd = job.fd
                if fd is None:
                    return
                data = os.pread(fd, min(HASH_BYTES, available - position), position)
                if not data:
                    raise Fatal("size_mismatch", f"{job.rel}: the part file is shorter than expected")
                digest.update(data)
                position += len(data)
            if digest.hexdigest() != job.sha256:
                job.discard()
                raise Fatal("hash_mismatch", job.rel)
            job.finish()
            log(f"verified {job.rel} ({job.size} bytes)")
        except Fatal as error:
            self.fail(error)
        except OSError as error:
            self.fail(Fatal("download_failed", f"{job.rel}: {error.strerror}"))

    # --- progress

    def bytes_done(self) -> int:
        return self.base_done + sum(j.bytes_done() for j in self.jobs)

    def files_done(self) -> int:
        return self.base_files + sum(1 for j in self.jobs if j.done)

    def write_progress(self, now: float) -> float:
        done = self.bytes_done()
        self.history.append((now, self.new_bytes))
        self.history = [(t, b) for (t, b) in self.history if now - t <= 10.0]
        oldest_time, oldest_bytes = self.history[0]
        window = max(now - oldest_time, 1e-6)
        mbps = (self.new_bytes - oldest_bytes) * 8 / 1e6 / window if len(self.history) > 1 else 0.0
        write_json_atomic(
            os.path.join(self.state_dir, "progress", self.label + ".json"),
            {
                "bytes_done": done,
                "bytes_total": self.base_total,
                "mbps": round(mbps, 1),
                "files_done": self.files_done(),
                "files_total": self.files_total,
                "at": int(time.time()),
            },
        )
        self.last_mbps = mbps
        return mbps

    def save_states(self) -> None:
        for job in self.jobs:
            if job.started and not job.done:
                try:
                    job.save_state()
                except OSError:
                    pass

    # --- the run

    def run(self) -> Tuple[str, int, float, float]:
        """Run to the end. Returns ("OK" or "LIMIT", new bytes, seconds, recent Mbps).

        The last number is the speed over the final ten seconds before the run
        stopped. It leaves out the first seconds, when the connections are still
        speeding up.
        """
        self.started_at = time.monotonic()
        threads = []
        for job in self.jobs:
            if job.started:
                self._start_hasher(job)
        for number in range(self.config.connections):
            thread = threading.Thread(target=self.worker, name=f"fetch-{number}", daemon=True)
            thread.start()
            threads.append(thread)
        last_progress = last_log = self.started_at
        self.write_progress(self.started_at)
        limit_reached = False
        while True:
            now = time.monotonic()
            if now - last_progress >= self.config.progress_every:
                last_progress = now
                self.write_progress(now)
                self.save_states()
            if all(j.done for j in self.jobs) or self.fatal is not None:
                break
            if self.time_limit and now - self.started_at >= self.time_limit:
                limit_reached = True
                break
            if now - last_log >= self.config.log_every:
                last_log = now
                total = max(self.base_total, 1)
                log(
                    f"{self.label}: {self.bytes_done() / 1e9:.1f} of {self.base_total / 1e9:.1f} GB "
                    f"({100 * self.bytes_done() / total:.0f} percent), {self.last_mbps:.0f} Mbps, "
                    f"{self.files_done()} of {self.files_total} files verified"
                )
            self.stop.wait(min(0.2, self.config.progress_every))
        recent_mbps = self.last_mbps
        self.stop.set()
        with self.cond:
            self.cond.notify_all()
        for job in self.jobs:
            with job.cond:
                job.cond.notify_all()
        for thread in threads:
            thread.join(timeout=10)
        for job in self.jobs:
            if job.hasher is not None and not (limit_reached and not job.done):
                job.hasher.join(timeout=5)
        self.save_states()
        for job in self.jobs:
            if not job.done:
                job.close()
        elapsed = time.monotonic() - self.started_at
        self.write_progress(time.monotonic())
        if self.fatal is not None:
            raise self.fatal
        status = "OK" if all(j.done for j in self.jobs) else "LIMIT"
        return status, self.new_bytes, elapsed, recent_mbps


# --- sub-commands ------------------------------------------------------------


def read_list(path: str) -> List[Tuple[str, str, int, str, str]]:
    rows = []
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.rstrip("\n")
            if not line:
                continue
            fields = line.split("\t")
            if len(fields) < 5:
                raise Fatal("bad_artifact_list", f"a line has {len(fields)} columns, expected 5")
            url, rel, size, sha, role = fields[:5]
            if not size.isdigit() or len(sha) != 64:
                raise Fatal("bad_artifact_list", f"{rel} has a bad size or hash")
            if rel.startswith("/") or ".." in rel.split("/"):
                raise Fatal("bad_artifact_list", f"{rel} is not a safe relative path")
            rows.append((url, rel, int(size), sha, role))
    return rows


def run_transfer(rows, selected, args, config: Config) -> Tuple[str, int, float, float]:
    """Download the selected rows. Rows that are not selected only count toward progress."""
    root, state_dir = args.root, args.state
    jobs: List[FileJob] = []
    base_done = base_files = 0
    total = sum(size for (_u, _r, size, _s, _o) in rows)
    selected_rels = {rel for (_u, rel, _s, _h, _o) in selected}
    for url, rel, size, sha, _role in rows:
        job = FileJob(url, rel, size, sha, root, state_dir)
        if rel not in selected_rels:
            if job.already_verified():
                base_done += size
                base_files += 1
            continue
        if job.already_verified():
            base_done += size
            base_files += 1
            continue
        os.makedirs(os.path.dirname(job.dest) or ".", exist_ok=True)
        if size == 0:
            open(job.dest, "wb").close()
            base_files += 1
            continue
        if not job.restore(config):
            job.plan(config)
        jobs.append(job)
    # Keep the order of the selection, which is the order of priority.
    order = {rel: number for number, (_u, rel, _s, _h, _o) in enumerate(selected)}
    jobs.sort(key=lambda j: order[j.rel])
    transfer = Transfer(
        jobs, config, state_dir, label=args.label, time_limit=args.time_limit,
        base_done=base_done, base_total=total, base_files=base_files, files_total=len(rows),
    )
    if not jobs:
        transfer.write_progress(time.monotonic())
        return "OK", 0, 0.0, 0.0
    return transfer.run()


def command_all(args, config: Config) -> Tuple[str, int, float, float]:
    rows = read_list(args.tsv)
    wanted = [r for r in args.roles.split(",") if r] if args.roles else []
    if wanted:
        selected = [row for role in wanted for row in rows if row[4] == role]
        # A role listed twice must not queue its files twice.
        seen: set = set()
        selected = [row for row in selected if not (row[1] in seen or seen.add(row[1]))]
    else:
        selected = list(rows)
    return run_transfer(rows, selected, args, config)


def command_one(args, config: Config) -> Tuple[str, int, float, float]:
    row = (args.url, args.rel, int(args.bytes), args.sha256, "file")
    return run_transfer([row], [row], args, config)


def command_join(args, config: Config) -> Tuple[str, int, float, float]:
    started = time.monotonic()
    out_path = os.path.join(args.root, args.out)
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    state_path = os.path.join(args.state, "steps", "join.json")
    saved = read_json(state_path) or {}
    appended = int(saved.get("appended", 0)) if saved.get("out") == args.out else 0
    offset = int(saved.get("offset", 0)) if appended else 0
    fd = os.open(out_path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        os.ftruncate(fd, offset)
        for number, rel in enumerate(args.parts):
            if number < appended:
                continue
            source = os.path.join(args.root, rel)
            while not os.path.exists(source):
                if failed_marker_exists(args.state):
                    raise Fatal("join_stopped", "the run failed while the join waited for " + rel)
                if args.watch_pid and not pid_alive(args.watch_pid):
                    raise Fatal("join_stopped", "the bootstrap ended while the join waited for " + rel)
                time.sleep(0.5)
            offset = append_file(source, fd, offset)
            appended = number + 1
            write_json_atomic(state_path, {"out": args.out, "appended": appended, "offset": offset})
            write_json_atomic(
                os.path.join(args.state, "progress", "join.json"),
                {"bytes_done": offset, "bytes_total": args.expect_bytes, "at": int(time.time())},
            )
            log(f"joined {rel} into {args.out} ({offset} of {args.expect_bytes} bytes)")
            if args.delete_inputs:
                os.makedirs(os.path.join(args.state, "consumed"), exist_ok=True)
                open(os.path.join(args.state, "consumed", marker_name(rel)), "w").close()
                os.unlink(source)
        if offset != args.expect_bytes:
            raise Fatal("size_mismatch", f"{args.out} holds {offset} bytes, the plan says {args.expect_bytes}")
    except OSError as error:
        raise Fatal("join_failed", f"{args.out}: {error.strerror}")
    finally:
        os.close(fd)
    return "OK", offset, time.monotonic() - started, 0.0


def append_file(source: str, out_fd: int, offset: int) -> int:
    """Copy a whole file to the end of the output. Returns the new output size."""
    src = os.open(source, os.O_RDONLY)
    try:
        remaining = os.fstat(src).st_size
        copy_range = getattr(os, "copy_file_range", None)
        position = 0
        while remaining > 0 and copy_range is not None:
            try:
                count = copy_range(src, out_fd, min(remaining, 1 << 30), position, offset + position)
            except OSError:
                copy_range = None  # the file system cannot copy inside the kernel
                break
            if count == 0:
                break
            position += count
            remaining -= count
        while remaining > 0:
            data = os.pread(src, min(remaining, 8 * MIB), position)
            if not data:
                raise Fatal("join_failed", f"{source} ended early")
            written = 0
            while written < len(data):
                written += os.pwrite(out_fd, data[written:], offset + position + written)
            position += len(data)
            remaining -= len(data)
        return offset + position
    finally:
        os.close(src)


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        with open(f"/proc/{pid}/stat", encoding="ascii", errors="replace") as handle:
            # The state letter follows the command name, which is in parentheses.
            return handle.read().rsplit(")", 1)[1].split()[0] != "Z"
    except (OSError, IndexError):
        return True


def command_watch(args, config: Config) -> Tuple[str, int, float, float]:
    """Write progress for a process that writes files, until that process ends."""
    started = time.monotonic()
    size = 0
    while True:
        size = 0
        for directory in args.dirs:
            for base, _dirs, names in os.walk(directory):
                for name in names:
                    try:
                        size += os.path.getsize(os.path.join(base, name))
                    except OSError:
                        pass
        write_json_atomic(
            os.path.join(args.state, "progress", args.label + ".json"),
            {"bytes_done": min(size, args.total), "bytes_total": args.total, "at": int(time.time())},
        )
        if not pid_alive(args.pid) or failed_marker_exists(args.state):
            break
        time.sleep(args.interval)
    return "OK", size, time.monotonic() - started, 0.0


def parser() -> argparse.ArgumentParser:
    main = argparse.ArgumentParser(description="Parallel, resumable, hash-checked downloads.")
    sub = main.add_subparsers(dest="command", required=True)

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--root", required=True, help="the data directory")
        p.add_argument("--state", required=True, help="the state directory")
        p.add_argument("--label", default="download", help="name of the progress file")
        p.add_argument("--time-limit", type=float, default=0.0, help="stop after this many seconds")

    a = sub.add_parser("all")
    common(a)
    a.add_argument("--tsv", required=True)
    a.add_argument("--roles", default="", help="comma separated roles, in priority order")
    o = sub.add_parser("one")
    common(o)
    o.add_argument("--url", required=True)
    o.add_argument("--rel", required=True)
    o.add_argument("--bytes", required=True)
    o.add_argument("--sha256", required=True)
    j = sub.add_parser("join")
    j.add_argument("--root", required=True)
    j.add_argument("--state", required=True)
    j.add_argument("--out", required=True)
    j.add_argument("--expect-bytes", type=int, required=True)
    j.add_argument("--delete-inputs", action="store_true")
    j.add_argument("--watch-pid", type=int, default=0)
    j.add_argument("parts", nargs="+")
    w = sub.add_parser("watch")
    w.add_argument("--state", required=True)
    w.add_argument("--label", required=True)
    w.add_argument("--total", type=int, required=True)
    w.add_argument("--pid", type=int, required=True)
    w.add_argument("--interval", type=float, default=5.0)
    w.add_argument("dirs", nargs="+")
    return main


def main(argv: Optional[List[str]] = None) -> int:
    args = parser().parse_args(argv)
    config = Config()
    handlers = {"all": command_all, "one": command_one, "join": command_join, "watch": command_watch}
    try:
        status, new_bytes, seconds, recent_mbps = handlers[args.command](args, config)
    except Fatal as error:
        detail = error.detail.replace("\t", " ").replace("\n", " ")
        print(f"FAIL\t{error.reason}\t{detail}", flush=True)
        return 4
    print(f"{status}\t{new_bytes}\t{seconds:.2f}\t{recent_mbps:.1f}", flush=True)
    return 0 if status == "OK" else 3


if __name__ == "__main__":
    sys.exit(main())
