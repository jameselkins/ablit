#!/usr/bin/env python3
"""Check launch bundles from an installed wheel without renting or using the network.

Run this file with the Python interpreter of a clean environment containing
the built wheel. Use -I so a checkout cannot satisfy the package imports.
"""

import io
import tarfile
from pathlib import Path

import ablit
from ablit import engines
from ablit.catalog import load_catalog
from ablit.catalog.loader import repo_root


def main() -> None:
    if repo_root() != Path(ablit.__file__).resolve().parent / "_data":
        raise RuntimeError("Install a built wheel in a clean environment before running this check.")
    catalog = load_catalog()
    if catalog.problems:
        raise RuntimeError(f"The installed catalog has invalid entries: {catalog.problems}")
    checked = 0
    for entry in catalog.entries:
        if entry.profile.engine not in engines.LAUNCHABLE_ENGINES:
            continue
        record = {"run_id": "install-check", "cap_usd": 1,
                  "min_download_mbps": entry.profile.min_download_mbps,
                  "offer": {"hourly_all_in": 0.1, "bandwidth_cost_per_gb": 0, "download_mbps": 1000}}
        with tarfile.open(fileobj=io.BytesIO(engines.bundle(entry, record))) as archive:
            for name in ("engine/bootstrap.sh", "engine/health.sh", "engine/common.sh", "engine/fetch.py",
                         "ablit_watchdog.py", "env", "artifacts.tsv"):
                member = archive.extractfile(name)
                if member is None or not member.read():
                    raise RuntimeError(f"The launch bundle for {entry.id} has no {name}.")
        print(f"Launch bundle passed: {entry.id}")
        checked += 1
    if not checked:
        raise RuntimeError("The installed catalog has no launchable entries.")


if __name__ == "__main__":
    main()
