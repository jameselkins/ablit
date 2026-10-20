"""Typed view of a catalog entry.

The JSON Schema in catalog/schema/v1.json is the contract for contributors.
These models are what the rest of the program reads after the schema check has
passed. The `bytes` and hash fields can be null in a file that has not been
filled in yet.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

Status = Literal["unverified", "boot-tested", "removed"]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Source(_Model):
    repo: str
    revision: str
    license: str
    base_model: str | None = None
    modification: Literal["none", "abliterated", "finetune", "quantized"]


class Artifact(_Model):
    repo: str | None = None
    revision: str | None = None
    path: str
    bytes: int | None
    sha256: str | None = None
    git_blob_id: str | None = None
    role: str


class GPU(_Model):
    vendor: Literal["nvidia", "amd"]
    count: int
    vram_gb_each: float
    vram_gb_total: float | None = None
    compute_capability_min: float | None = None


class Storage(_Model):
    random_read_required: bool = False
    disk_gb_claimed: float | None = None


class Profile(_Model):
    engine: Literal["vllm", "llamacpp", "xolotl"]
    engine_lock: str
    gpu: GPU
    host_ram_gb: float
    host_ram_gb_recommended: float | None = None
    storage: Storage
    context_tokens: int
    concurrency: int
    port: int
    startup_hours_estimate: float
    min_download_mbps: float


class StagingStep(_Model):
    step: Literal["download", "join", "engram-extract"]
    artifacts: str | list[str] | None = None
    inputs: list[str] | None = None
    output_bytes: int | None = None
    keeps_inputs_until_done: bool = True
    delete_inputs_after: bool = False


class Entry(_Model):
    schema_version: int
    id: str
    display_name: str
    source: Source
    artifacts: list[Artifact]
    staging: list[StagingStep]
    profile: Profile
    sources: list[Any]
    tests: list[dict[str, Any]]
    status: Status
    notes: list[str] = []

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Entry":
        """Build an entry from parsed YAML. The caller has already run the schema check."""
        data = dict(data)
        data["schema_version"] = data.pop("schema")
        return cls(**data)

    def artifact_repo(self, artifact: Artifact) -> str:
        return artifact.repo or self.source.repo

    def artifact_revision(self, artifact: Artifact) -> str:
        return artifact.revision or self.source.revision
