#!/usr/bin/env python3
"""Check catalog entries, and fill in artifact sizes and hashes.

Usage:
    python3 tools/verify_entry.py [--offline] [--fill] [FILE ...]

With no files, every entry in catalog/models/ is checked.

Offline checks need PyYAML and pydantic, which the ablit package already depends on.
They cover the schema, the source URLs, the
source for every number in `profile`, and the staging plan. `--offline` skips
the network checks, so CI on a fork can run the schema part.

Online checks compare each artifact with the Hugging Face API at the pinned
revision, using `?blobs=true`. The tree API is paged at 50 entries, so it is not
used. Large files are checked by size and LFS SHA-256. Files without an LFS
record are checked by size and Git blob ID, and the script downloads them to
compute a SHA-256 and to confirm the blob ID from the content.

`--fill` writes sizes and hashes into each file. It needs the network.

Only anonymous GET requests are made, and only to huggingface.co.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "cli"))

import yaml  # noqa: E402

from ablit.catalog import check_entry, gguf, load_entry_file  # noqa: E402
from ablit.catalog.dump import dump_entry, leading_comments  # noqa: E402
from ablit.catalog.models import Entry  # noqa: E402

API = "https://huggingface.co/api/models/{repo}/revision/{rev}?blobs=true"
RESOLVE = "https://huggingface.co/{repo}/resolve/{rev}/{path}"
USER_AGENT = "ablit-verify-entry/0 (anonymous metadata check)"


def _host_allowed(host: str) -> bool:
    host = host.lower()
    return host in ("huggingface.co", "hf.co") or host.endswith((".huggingface.co", ".hf.co"))


class _SameSiteRedirects(urllib.request.HTTPRedirectHandler):
    """Follow redirects only when they stay on Hugging Face hosts."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        target = urllib.parse.urlparse(urllib.parse.urljoin(req.full_url, newurl))
        if target.scheme != "https" or not _host_allowed(target.hostname or ""):
            raise urllib.error.URLError(f"refusing to follow a redirect to {target.hostname}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class HFClient:
    """Anonymous GET requests to huggingface.co with a few retries."""

    def __init__(self, retries: int = 3, timeout: float = 120.0) -> None:
        self._opener = urllib.request.build_opener(_SameSiteRedirects)
        self._retries = retries
        self._timeout = timeout
        self._api_cache: dict[tuple[str, str], dict[str, Any]] = {}
        self._file_cache: dict[tuple[str, str, str], bytes] = {}

    def _get(self, url: str, headers: dict[str, str] | None = None) -> bytes:
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme != "https" or not _host_allowed(parsed.hostname or ""):
            raise ValueError(f"refusing to contact {parsed.hostname}")
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
        last: Exception | None = None
        for attempt in range(self._retries):
            try:
                with self._opener.open(request, timeout=self._timeout) as response:
                    return response.read()
            except urllib.error.HTTPError as error:
                if error.code in (401, 403, 404):
                    raise
                last = error
            except (urllib.error.URLError, TimeoutError) as error:
                last = error
            time.sleep(2 ** attempt)
        assert last is not None
        raise last

    def revision(self, repo: str, rev: str) -> dict[str, Any]:
        key = (repo, rev)
        if key not in self._api_cache:
            url = API.format(repo=urllib.parse.quote(repo, safe="/"), rev=rev)
            self._api_cache[key] = json.loads(self._get(url))
        return self._api_cache[key]

    def file(self, repo: str, rev: str, path: str) -> bytes:
        key = (repo, rev, path)
        if key not in self._file_cache:
            url = RESOLVE.format(
                repo=urllib.parse.quote(repo, safe="/"), rev=rev, path=urllib.parse.quote(path, safe="/")
            )
            self._file_cache[key] = self._get(url)
        return self._file_cache[key]

    def head_bytes(self, repo: str, rev: str, path: str, count: int) -> bytes:
        url = RESOLVE.format(
            repo=urllib.parse.quote(repo, safe="/"), rev=rev, path=urllib.parse.quote(path, safe="/")
        )
        return self._get(url, {"Range": f"bytes=0-{count - 1}"})


def git_blob_id(content: bytes) -> str:
    return hashlib.sha1(b"blob " + str(len(content)).encode() + b"\0" + content).hexdigest()


def _coordinates(data: dict[str, Any], artifact: dict[str, Any]) -> tuple[str, str]:
    return (
        artifact.get("repo") or data["source"]["repo"],
        artifact.get("revision") or data["source"]["revision"],
    )


def _siblings(info: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {s["rfilename"]: s for s in info.get("siblings", [])}


def fill_entry(data: dict[str, Any], client: HFClient) -> dict[str, Any]:
    """Return a copy of the entry with sizes and hashes from Hugging Face."""
    out = json.loads(json.dumps(data))
    for artifact in out["artifacts"]:
        repo, rev = _coordinates(out, artifact)
        sibling = _siblings(client.revision(repo, rev)).get(artifact["path"])
        if sibling is None:
            raise SystemExit(f"{out['id']}: {repo}@{rev[:8]} has no file {artifact['path']}")
        lfs = sibling.get("lfs")
        rebuilt: dict[str, Any] = {}
        if "repo" in artifact:
            rebuilt["repo"] = artifact["repo"]
        if "revision" in artifact:
            rebuilt["revision"] = artifact["revision"]
        rebuilt["path"] = artifact["path"]
        if lfs:
            rebuilt["bytes"] = int(lfs["size"])
            rebuilt["sha256"] = lfs["sha256"]
        else:
            content = client.file(repo, rev, artifact["path"])
            rebuilt["bytes"] = len(content)
            rebuilt["sha256"] = hashlib.sha256(content).hexdigest()
            rebuilt["git_blob_id"] = sibling["blobId"]
        rebuilt["role"] = artifact["role"]
        artifact.clear()
        artifact.update(rebuilt)

    # A join writes one file of exactly the size of its inputs. Fill it in when
    # the entry left the number out. Other steps keep the number the entry gives.
    entry = Entry.from_dict(out)
    for step_data, step in zip(out["staging"], entry.staging, strict=False):
        if step.step == "join" and step.output_bytes is None:
            total = 0
            for selector in step.inputs or []:
                matches = [a for a in entry.artifacts if a.path == selector] or [
                    a for a in entry.artifacts if a.role == selector
                ]
                total += sum(a.bytes or 0 for a in matches)
            step_data["output_bytes"] = total
    return out


def verify_online(data: dict[str, Any], client: HFClient, engine_lock: dict[str, Any] | None = None) -> list[str]:
    """Compare an entry with Hugging Face. Return a list of problems."""
    problems: list[str] = []
    entry_id = data["id"]

    source = data["source"]
    try:
        info = client.revision(source["repo"], source["revision"])
    except (urllib.error.HTTPError, urllib.error.URLError, ValueError, TimeoutError) as error:
        return [f"{entry_id}: cannot read {source['repo']}@{source['revision'][:8]}: {error}"]
    if info.get("sha") != source["revision"]:
        problems.append(f"{entry_id}: the API reports revision {info.get('sha')} for {source['repo']}")
    if info.get("gated") or info.get("private"):
        problems.append(f"{entry_id}: {source['repo']} is gated or private, so it cannot be fetched anonymously")
    card_license = (info.get("cardData") or {}).get("license")
    if card_license and str(card_license).lower() != str(source["license"]).lower():
        problems.append(
            f"{entry_id}: source.license is {source['license']} but the model card says {card_license}"
        )

    for artifact in data["artifacts"]:
        repo, rev = _coordinates(data, artifact)
        label = f"{entry_id}: {repo}@{rev[:8]}/{artifact['path']}"
        try:
            sibling = _siblings(client.revision(repo, rev)).get(artifact["path"])
        except (urllib.error.HTTPError, urllib.error.URLError, ValueError, TimeoutError) as error:
            problems.append(f"{label}: cannot read the repo: {error}")
            continue
        if sibling is None:
            problems.append(f"{label}: the file is missing at that revision")
            continue
        lfs = sibling.get("lfs")
        api_size = int(lfs["size"]) if lfs else int(sibling["size"])
        if artifact["bytes"] is None:
            problems.append(f"{label}: size is not filled in. Run tools/verify_entry.py --fill")
            continue
        if artifact["bytes"] != api_size:
            problems.append(f"{label}: size is {artifact['bytes']} but Hugging Face reports {api_size}")
        if lfs:
            if artifact.get("sha256") != lfs["sha256"]:
                problems.append(f"{label}: sha256 does not match the LFS record")
        else:
            if artifact.get("git_blob_id") != sibling.get("blobId"):
                problems.append(f"{label}: git_blob_id does not match {sibling.get('blobId')}")
            else:
                try:
                    content = client.file(repo, rev, artifact["path"])
                except (urllib.error.HTTPError, urllib.error.URLError, ValueError, TimeoutError) as error:
                    problems.append(f"{label}: cannot download to check the hash: {error}")
                    continue
                if git_blob_id(content) != sibling.get("blobId"):
                    problems.append(f"{label}: the downloaded content does not match the Git blob ID")
                if artifact.get("sha256") and hashlib.sha256(content).hexdigest() != artifact["sha256"]:
                    problems.append(f"{label}: sha256 does not match the downloaded content")

    if data["profile"]["engine"] == "llamacpp":
        problems.extend(_check_gguf_architecture(data, client, engine_lock))
    return problems


def _check_gguf_architecture(
    data: dict[str, Any], client: HFClient, engine_lock: dict[str, Any] | None
) -> list[str]:
    supported = (engine_lock or {}).get("supported_architectures") or []
    if not supported:
        print(
            f"note: {data['id']}: engines/llamacpp/lock.yaml lists no supported architectures yet, "
            "so the GGUF architecture check was skipped",
            file=sys.stderr,
        )
        return []
    problems: list[str] = []
    for artifact in data["artifacts"]:
        if artifact["role"] != "weight" or not artifact["path"].endswith(".gguf"):
            continue
        repo, rev = _coordinates(data, artifact)
        size = 1 << 20
        while True:
            try:
                head = client.head_bytes(repo, rev, artifact["path"], size)
                arch = gguf.read_architecture(head)
                break
            except gguf.NeedMoreBytes:
                size *= 4
                if size > 1 << 28:
                    return [f"{data['id']}: the GGUF header of {artifact['path']} is larger than 256 MB"]
            except (gguf.NotGguf, urllib.error.URLError, ValueError) as error:
                return [f"{data['id']}: cannot read the GGUF header of {artifact['path']}: {error}"]
        if arch not in supported:
            problems.append(
                f"{data['id']}: GGUF architecture {arch!r} is not in the pinned llama.cpp build's list"
            )
    return problems


def _entry_files(args: argparse.Namespace) -> list[Path]:
    if args.files:
        return [Path(f) for f in args.files]
    return sorted((ROOT / "catalog" / "models").glob("*.yaml"))


def _engine_lock(data: dict[str, Any]) -> dict[str, Any] | None:
    engine = data.get("profile", {}).get("engine")
    name = {"vllm": "vllm", "llamacpp": "llamacpp", "xolotl": "xolotl"}.get(engine or "")
    if not name:
        return None
    lock = ROOT / "engines" / name / "lock.yaml"
    if not lock.is_file():
        return None
    with open(lock, encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def main(argv: list[str] | None = None, client: HFClient | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check catalog entries against the schema and Hugging Face.")
    parser.add_argument("files", nargs="*", help="entry files. Default is every file in catalog/models/")
    parser.add_argument("--offline", action="store_true", help="skip the network checks")
    parser.add_argument("--fill", action="store_true", help="write artifact sizes and hashes into each file")
    args = parser.parse_args(argv)
    if args.offline and args.fill:
        parser.error("--fill needs the network, so it cannot be combined with --offline")

    files = _entry_files(args)
    if not files:
        print("no entry files found", file=sys.stderr)
        return 1

    client = client or (None if args.offline else HFClient())
    failed = False
    for path in files:
        label = path.name
        try:
            data = load_entry_file(path)
        except Exception as error:  # a broken file is reported, and the rest still run
            print(f"FAIL {label}: {error}")
            failed = True
            continue

        if args.fill:
            schema_problems = check_entry(data)
            if schema_problems:
                print(f"FAIL {label}")
                for problem in schema_problems:
                    print(f"  {problem}")
                failed = True
                continue
            text = path.read_text(encoding="utf-8")
            data = fill_entry(data, client)  # type: ignore[arg-type]
            path.write_text(dump_entry(data, leading_comments(text)), encoding="utf-8")
            print(f"filled {label}")

        problems = check_entry(data)
        if not args.offline and not problems:
            problems.extend(verify_online(data, client, _engine_lock(data)))  # type: ignore[arg-type]
        if problems:
            print(f"FAIL {label}")
            for problem in problems:
                print(f"  {problem}")
            failed = True
        else:
            print(f"ok   {label}" + (" (offline checks only)" if args.offline else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
