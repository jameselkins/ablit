#!/usr/bin/env python3
"""Run `ablit plan --json` for every catalog entry against the live marketplaces.

Usage:
    python3 tools/live_plan.py OUTPUT_DIR [--hours N]

This is a manual smoke check. CI never runs it, and the script refuses to start
when it sees a CI environment variable. It makes anonymous, read-only search
requests and nothing else:

1. It sends no API keys, even when keys are set in the environment.
2. It allows only POST requests to console.vast.ai/api/v0/bundles/ and
   api.runpod.io/graphql, and it refuses any GraphQL text that holds a mutation.
3. It leaves at least one second between requests.

OUTPUT_DIR receives one JSON file per entry (the raw output of `ablit plan
--json`), `index.json` with the exit code of each plan, and `requests.log` with
one line per request (method, host, path, status, time and size, with no
bodies). The script refuses to write inside this repository, because marketplace
price data must not be published.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "cli"))

from ablit.backends import RunPodBackend, VastBackend  # noqa: E402
from ablit.catalog import load_catalog  # noqa: E402
from ablit.cli import main  # noqa: E402

ALLOWED = {
    ("console.vast.ai", "/api/v0/bundles/"),
    ("api.runpod.io", "/graphql"),
}
MIN_SECONDS_BETWEEN_REQUESTS = 1.1
CI_VARIABLES = ("CI", "GITHUB_ACTIONS", "BUILDKITE", "GITLAB_CI", "JENKINS_URL", "TF_BUILD")


class ReadOnlyTransport(httpx.BaseTransport):
    """Sends only allowed read-only searches, at most one request per second, and logs each one."""

    def __init__(self, log_path: Path) -> None:
        self._inner = httpx.HTTPTransport()
        self._log = log_path
        self._last = 0.0
        self.count = 0

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        host, path = request.url.host, request.url.path
        if request.method != "POST" or (host, path) not in ALLOWED:
            raise RuntimeError(f"refusing {request.method} {host}{path}: not an allowed search endpoint")
        if "authorization" in request.headers:
            raise RuntimeError("refusing to send an Authorization header in an anonymous run")
        if host == "api.runpod.io":
            query = json.loads(request.content).get("query", "")
            if not query.lstrip().startswith("query") or "mutation" in query.lower():
                raise RuntimeError("refusing a GraphQL request that is not a plain query")
        wait = self._last + MIN_SECONDS_BETWEEN_REQUESTS - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        started = time.monotonic()
        self._last = started
        response = self._inner.handle_request(request)
        body = response.read()
        self.count += 1
        line = {
            "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "method": request.method,
            "host": host,
            "path": path,
            "request_bytes": len(request.content),
            "status": response.status_code,
            "response_bytes": len(body),
            "seconds": round(time.monotonic() - started, 2),
        }
        with self._log.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(line) + "\n")
        return response

    def close(self) -> None:
        self._inner.close()


def inside(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
    except ValueError:
        return False
    return True


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("output_dir", help="a directory outside this repository")
    parser.add_argument("--hours", type=float, default=1.0, help="session length used to rank offers. Default 1.")
    return parser.parse_args(argv)


def run(output_dir: Path, hours: float) -> int:
    output_dir.mkdir(parents=True, exist_ok=True)
    log_path = output_dir / "requests.log"
    transport = ReadOnlyTransport(log_path)
    client = httpx.Client(transport=transport, timeout=httpx.Timeout(60.0))
    backends = {"runpod": RunPodBackend(client), "vast": VastBackend(client)}
    entries = load_catalog().entries
    index: dict[str, Any] = {}
    for entry in entries:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            # An empty environment means no API key is sent, whatever the shell holds.
            code = main(["plan", entry.id, "--json", "--hours", f"{hours:g}"], backends=backends, environ={})
        text = buffer.getvalue()
        (output_dir / f"{entry.id}.json").write_text(text, encoding="utf-8")
        index[entry.id] = {"exit_code": code, "bytes": len(text)}
        print(f"{entry.id}: exit {code}", file=sys.stderr)
    (output_dir / "index.json").write_text(json.dumps(index, indent=2) + "\n", encoding="utf-8")
    print(f"{transport.count} requests, log in {log_path}", file=sys.stderr)
    return 0


def cli(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    ci = [name for name in CI_VARIABLES if os.environ.get(name)]
    if ci:
        print(f"live_plan: refusing to run because {ci[0]} is set. This script is never run in CI.", file=sys.stderr)
        return 2
    output_dir = Path(args.output_dir).expanduser()
    if inside(output_dir, ROOT):
        print("live_plan: refusing to write inside the repository. Choose a directory outside it.", file=sys.stderr)
        return 2
    if not args.hours > 0:
        print("live_plan: --hours must be above zero", file=sys.stderr)
        return 2
    return run(output_dir, args.hours)


if __name__ == "__main__":
    raise SystemExit(cli())
