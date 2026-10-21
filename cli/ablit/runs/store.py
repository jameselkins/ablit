"""Keep run records on disk, with durable replacement and advisory locks."""

from __future__ import annotations

import fcntl
import json
import os
import re
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

RUN_STATES = (
    "creating", "running", "termination_requested", "resources_terminated",
    "billing_stopped_confirmed",
)


class RunError(Exception):
    """A run cannot be selected or changed safely."""


def state_dir() -> Path:
    """Follow XDG on Unix and Application Support on macOS."""
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "ablit"
    return Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local" / "state"))) / "ablit"


def atomic_json(path: Path, data: dict) -> None:
    """Flush a private temporary file, replace the record, then flush its directory."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)


class RunStore:
    def __init__(self, root: Path | None = None) -> None:
        self.root = root or state_dir()
        self.runs = self.root / "runs"

    def path(self, run_id: str) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,100}", run_id):
            raise RunError("The run ID contains invalid characters.")
        return self.runs / f"{run_id}.json"

    def load(self, run_id: str) -> dict:
        try:
            data = json.loads(self.path(run_id).read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise RunError(f"Cannot read run {run_id}.") from error
        if data.get("run_id") != run_id or data.get("state") not in RUN_STATES:
            raise RunError(f"Run {run_id} has an unknown record shape.")
        return data

    def save(self, data: dict) -> None:
        atomic_json(self.path(data["run_id"]), data)

    def all(self) -> list[dict]:
        return [self.load(p.stem) for p in sorted(self.runs.glob("*.json"))]

    def select(self, run_id: str | None) -> dict:
        if run_id:
            return self.load(run_id)
        records = self.all()
        active = [r for r in records if r["state"] != "billing_stopped_confirmed"]
        candidates = active or records[-1:]
        if len(candidates) != 1:
            raise RunError("Name a run ID. Use `ablit status --all` to list runs.")
        return candidates[0]

    @contextmanager
    def lock(self, run_id: str, wait: float = 0.0):
        """Take the run lock. With `wait`, keep trying for that many seconds.

        `down` and `status` wait, because an attached `up` holds the lock for a
        few seconds on every poll, and `down` must work while `up` is attached.
        """
        path = self.path(run_id).with_suffix(".lock")
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            deadline = time.monotonic() + wait
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError as error:
                    if time.monotonic() >= deadline:
                        raise RunError(f"Run {run_id} is busy in another command.") from error
                    time.sleep(0.2)
            yield
        finally:
            os.close(fd)

    def active_model(self, model: str) -> list[dict]:
        return [r for r in self.all() if r["model_id"] == model and r["state"] != "billing_stopped_confirmed"]

    def denied_hosts(self) -> set[str]:
        path = self.root / "deny.json"
        if not path.exists():
            return set()
        return set(json.loads(path.read_text())["hosts"])

    def deny(self, host_id: str, reason: str) -> None:
        with self.lock("deny"):
            path = self.root / "deny.json"
            data = json.loads(path.read_text()) if path.exists() else {"hosts": {}}
            data["hosts"][host_id] = reason
            atomic_json(path, data)
