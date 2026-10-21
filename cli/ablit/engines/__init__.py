"""Render the reviewed llama.cpp templates and non-secret configuration."""

from __future__ import annotations

import io
import shlex
import tarfile
from pathlib import PurePosixPath
from urllib.parse import quote

import httpx
import yaml

from ablit.catalog.loader import repo_root
from ablit.catalog.staging import compute_staging


def engine_lock(entry) -> dict:
    root = repo_root()
    if root is None:
        raise ValueError("The engine files were not found next to this package. Reinstall ablit.")
    lock = yaml.safe_load((root / "engines" / entry.profile.engine / "lock.yaml").read_text())
    if lock["name"] != entry.profile.engine_lock:
        raise ValueError("The entry and image lock disagree.")
    return lock


def check_artifacts(entry) -> None:
    """Check pinned public metadata with an anonymous, mockable HTTP client."""
    with httpx.Client(timeout=30, trust_env=False) as client:
        for repo, revision in {(entry.artifact_repo(a), entry.artifact_revision(a)) for a in entry.artifacts}:
            # https://huggingface.co/docs/hub/api#get-apimodelsrepo_idrevisionrevision
            response = client.get(f"https://huggingface.co/api/models/{repo}/revision/{revision}",
                                  params={"blobs": "true"})
            response.raise_for_status()
            info = response.json()
            if (not isinstance(info, dict) or info.get("private") is not False
                    or info.get("gated") or info.get("sha") != revision
                    or not isinstance(info.get("siblings"), list)):
                raise ValueError("Every pinned artifact must be public and ungated.")
            files = {}
            for item in info["siblings"]:
                if not isinstance(item, dict) or not isinstance(item.get("rfilename"), str):
                    raise ValueError("The artifact metadata has an unknown shape.")
                files[item["rfilename"]] = item
            for artifact in entry.artifacts:
                if (entry.artifact_repo(artifact), entry.artifact_revision(artifact)) != (repo, revision):
                    continue
                found = files.get(artifact.path)
                if found is None or type(found.get("size")) is not int or found["size"] != artifact.bytes:
                    raise ValueError("A pinned artifact is missing or its size changed.")
                lfs = found.get("lfs")
                if lfs is not None:
                    if not isinstance(lfs, dict) or lfs.get("sha256") != artifact.sha256:
                        raise ValueError("A pinned artifact hash changed.")
                elif not artifact.git_blob_id or found.get("blobId") != artifact.git_blob_id:
                    raise ValueError("A pinned artifact blob changed.")
                if not artifact.sha256:
                    raise ValueError("Every artifact needs a SHA-256 before launch.")


# Engines that a paid launch can start. Each one has its own bootstrap.sh and
# health.sh under engines/<name>/, and all of them share engines/lib.
LAUNCHABLE_ENGINES = ("llamacpp", "xolotl")


def _staging_flags(entry) -> dict:
    """Say, from the staging plan, which downloaded inputs the plan deletes."""
    flags = {"ABLIT_DELETE_PARTS": 0, "ABLIT_DELETE_ENGRAM_SOURCE": 0}
    for step in entry.staging:
        if step.step == "join" and step.delete_inputs_after:
            flags["ABLIT_DELETE_PARTS"] = 1
        if step.step == "engram-extract" and step.delete_inputs_after:
            flags["ABLIT_DELETE_ENGRAM_SOURCE"] = 1
    return flags


def bundle(entry, record: dict) -> bytes:
    """Build a tar archive with no endpoint or marketplace credentials."""
    root = repo_root()
    p = entry.profile
    offer = record["offer"]
    if p.engine not in LAUNCHABLE_ENGINES:
        raise ValueError(f"A paid launch has no bootstrap for the {p.engine} engine.")
    lock = engine_lock(entry)
    values = {
        "ABLIT_RUN_ID": record["run_id"], "ABLIT_MODEL_ID": entry.id,
        "ABLIT_SERVED_NAME": entry.id, "ABLIT_PORT": p.port,
        "ABLIT_CAP_USD": record["cap_usd"], "ABLIT_HOURLY_RATE": offer["hourly_all_in"],
        "ABLIT_TRANSFER_PER_GB": offer["bandwidth_cost_per_gb"],
        "ABLIT_ADVERTISED_MBPS": offer["download_mbps"],
        "ABLIT_GPU_COUNT": p.gpu.count, "ABLIT_MIN_VRAM_MB_EACH": p.gpu.vram_gb_each * 1e9 / 2**20,
        "ABLIT_MIN_HOST_RAM_GB": p.host_ram_gb,
        "ABLIT_MIN_DISK_GB": compute_staging(entry).peak_gb + float(lock.get("disk_margin_gb", 0.0)),
        "ABLIT_MIN_DOWNLOAD_MBPS": record["min_download_mbps"],
        "ABLIT_CONTEXT_TOKENS": p.context_tokens,
        "ABLIT_RANDOM_READ_REQUIRED": int(p.storage.random_read_required),
        "ABLIT_LIB": "/ablit/engine/common.sh",
    }
    if p.gpu.compute_capability_min is not None:
        values["ABLIT_MIN_CC"] = p.gpu.compute_capability_min
    # The box repeats the checks that the offer search already made, because a
    # wrong GPU generation or an old driver fails late and costs rented minutes.
    if lock.get("cuda_architectures"):
        values["ABLIT_CUDA_ARCHITECTURES"] = " ".join(str(n) for n in lock["cuda_architectures"])
    if lock.get("min_driver_major"):
        values["ABLIT_MIN_DRIVER_MAJOR"] = lock["min_driver_major"]
    if p.engine == "xolotl":
        values.update(_staging_flags(entry))
        if lock.get("min_memlock_kb"):
            values["ABLIT_MIN_MEMLOCK_KB"] = lock["min_memlock_kb"]
    lines = []
    for artifact in entry.artifacts:
        path = PurePosixPath(artifact.path)
        if path.is_absolute() or ".." in path.parts or any(c in artifact.path for c in "\n\t\r"):
            raise ValueError("Artifact path is unsafe.")
        url = "https://huggingface.co/{}/resolve/{}/{}".format(
            entry.artifact_repo(artifact), entry.artifact_revision(artifact), quote(artifact.path, safe="/"))
        lines.append(f"{url}\t{artifact.path}\t{artifact.bytes}\t{artifact.sha256}\t{artifact.role}\n")
    files = {"env": "".join(f"{k}={shlex.quote(str(v))}\n" for k, v in values.items()).encode(),
             "artifacts.tsv": "".join(lines).encode()}
    for name in ("bootstrap.sh", "health.sh"):
        files[f"engine/{name}"] = (root / "engines" / p.engine / name).read_bytes()
    files["engine/common.sh"] = (root / "engines" / "lib" / "common.sh").read_bytes()
    files["engine/fetch.py"] = (root / "engines" / "lib" / "fetch.py").read_bytes()
    files["ablit_watchdog.py"] = (root / "cli" / "ablit" / "watchdog" / "ablit_watchdog.py").read_bytes()
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as archive:
        for name, content in files.items():
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(content), 0o700 if name.endswith(".sh") else 0o600
            archive.addfile(info, io.BytesIO(content))
    return output.getvalue()


def health(base: str, key: str, model: str) -> None:
    """Exercise authentication, chat and streaming through the SSH tunnel."""
    with httpx.Client(base_url=base, timeout=60, headers={"Authorization": f"Bearer {key}"}, trust_env=False) as client:
        result = client.get("/v1/models")
        result.raise_for_status()
        if not isinstance(result.json().get("data"), list):
            raise ValueError("The models response is invalid.")
        body = {"model": model, "messages": [{"role": "user", "content": "Hello"}], "max_tokens": 1}
        reply = client.post("/v1/chat/completions", json=body)
        reply.raise_for_status()
        if not reply.json().get("choices"):
            raise ValueError("The completion response has no choices.")
        with client.stream("POST", "/v1/chat/completions", json={**body, "stream": True}) as stream:
            stream.raise_for_status()
            lines = list(stream.iter_lines())
            if not any(line.startswith("data: ") for line in lines) or "data: [DONE]" not in lines:
                raise ValueError("The stream did not finish.")
        for token in (None, "invented-wrong-health-token"):
            headers = {"Authorization": f"Bearer {token}"} if token else {}
            with httpx.Client(timeout=30, trust_env=False) as unauthenticated:
                if unauthenticated.get(base + "/v1/models", headers=headers).status_code not in (401, 403):
                    raise ValueError("The endpoint accepted a missing or wrong key.")
