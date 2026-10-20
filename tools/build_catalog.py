#!/usr/bin/env python3
"""Compile catalog/models/*.yaml into catalog/dist/catalog.json and print its SHA-256.

The output is deterministic. Keys are sorted, entries are ordered by ID, and
the file has no spaces or trailing newline, so the same entries always give the
same digest. Each CLI release pins this digest, and every quote records it.

Usage:
    python3 tools/build_catalog.py
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "cli"))

from ablit.catalog import check_entry, load_entry_file  # noqa: E402

VERSION_PATTERN = re.compile(r"^\d{4}\.\d{2}\.\d{2}\.\d+$")
SCHEMA_VERSION = 1


def build(root: Path = ROOT) -> tuple[bytes, list[str]]:
    """Return the compiled catalog bytes and a list of problems."""
    problems: list[str] = []
    version_file = root / "catalog" / "VERSION"
    version = version_file.read_text(encoding="utf-8").strip() if version_file.is_file() else ""
    if not VERSION_PATTERN.match(version):
        problems.append(f"catalog/VERSION must look like YYYY.MM.DD.N, got {version!r}")

    entries = []
    for path in sorted((root / "catalog" / "models").glob("*.yaml")):
        data = load_entry_file(path)
        found = check_entry(data)
        if found:
            problems.extend(f"{path.name}: {problem}" for problem in found)
            continue
        if path.stem != data["id"]:
            problems.append(f"{path.name}: the file name must match the entry id {data['id']!r}")
        entries.append(data)
    entries.sort(key=lambda e: e["id"])

    document = {"catalog_version": version, "schema": SCHEMA_VERSION, "entries": entries}
    payload = json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return payload.encode("utf-8"), problems


def main() -> int:
    payload, problems = build()
    if problems:
        for problem in problems:
            print(f"error: {problem}", file=sys.stderr)
        return 1
    out = ROOT / "catalog" / "dist" / "catalog.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(payload)
    print(hashlib.sha256(payload).hexdigest())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
