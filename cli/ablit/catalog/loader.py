"""Find, read, check and look up catalog entries."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

from ablit.catalog import schema_check, sources, staging
from ablit.catalog.models import Entry


class CatalogError(Exception):
    """A catalog file or directory cannot be used."""


class ModelNotInCatalog(CatalogError):
    """The requested model matches no entry."""


class AmbiguousModel(CatalogError):
    """A repo matches several entries, and the caller did not pick one."""

    def __init__(self, model: str, candidates: list[str]) -> None:
        super().__init__(
            f"{model} matches {len(candidates)} entries. Pick one with --variant: " + ", ".join(candidates)
        )
        self.candidates = candidates


def repo_root() -> Path | None:
    """Return the directory that holds `catalog/` and `engines/`.

    In a source checkout that is the repository root. An installed wheel
    carries copies of both directories in `ablit/_data/`.
    """
    here = Path(__file__).resolve()
    for candidate in (here.parents[3], here.parents[1] / "_data"):
        if (candidate / "catalog" / "schema" / "v1.json").is_file():
            return candidate
    return None


def default_catalog_dir() -> Path | None:
    """Return the directory of catalog entries, or None when none can be found.

    The environment variable ABLIT_CATALOG wins. Otherwise the checkout that
    holds this package is used.
    """
    env = os.environ.get("ABLIT_CATALOG")
    if env:
        return Path(env)
    root = repo_root()
    return root / "catalog" / "models" if root else None


def schema_path() -> Path:
    root = repo_root()
    if root is None:
        raise CatalogError("catalog/schema/v1.json was not found next to this package")
    return root / "catalog" / "schema" / "v1.json"


def load_entry_file(path: str | Path) -> dict[str, Any]:
    """Read one YAML file and return its top-level mapping."""
    try:
        with open(path, encoding="utf-8") as handle:
            data = yaml.safe_load(handle)
    except UnicodeError as error:
        raise CatalogError(f"{path}: the file must be UTF-8 text") from error
    except RecursionError as error:
        raise CatalogError(f"{path}: the YAML nesting is too deep") from error
    except (OSError, ValueError, yaml.YAMLError) as error:
        raise CatalogError(f"{path}: {error}") from error
    if not isinstance(data, dict):
        raise CatalogError(f"{path}: the file must hold a YAML mapping")
    return data


def check_entry(data: dict[str, Any], schema: dict[str, Any] | None = None) -> list[str]:
    """Run every check that needs no network. Return a list of problems.

    The checks are the schema, the source URLs, the source coverage of profile
    numbers, and the staging plan against the entry's own disk claim.
    """
    schema = schema or schema_check.load_schema(schema_path())
    problems = schema_check.validate(data, schema)
    if problems:
        return problems

    for url in sources.bad_source_urls(data):
        problems.append(f"sources: {url!r} is not a public https URL")
    for path in sources.uncovered_numbers(data):
        problems.append(f"{path} has no source (add it to `covers` in a sources entry)")

    seen: set[PurePosixPath] = set()
    checkpoints: dict[str, str] = {}
    for artifact in data["artifacts"]:
        # Every repo downloads into the same model directory. Normalize dots and
        # repeated slashes so alternate spellings cannot share a destination.
        destination = PurePosixPath(artifact["path"])
        if destination in seen:
            problems.append(f"artifacts: {artifact['path']} is listed twice as a download destination")
        seen.add(destination)
        # Match marker_name in fetch.py and ablit_mark_consumed in common.sh.
        # Those checkpoints use the original path, including dots and repeated slashes.
        artifact_path = artifact["path"]
        checkpoint = artifact_path.replace("/", "%2F")
        previous = checkpoints.get(checkpoint)
        if previous is not None and previous != artifact_path:
            problems.append(f"artifacts: {previous!r} and {artifact_path!r} share a download checkpoint")
        checkpoints.setdefault(checkpoint, artifact_path)
    for destination in sorted(seen):
        for parent in destination.parents:
            if parent in seen:
                problems.append(
                    f"artifacts: {str(destination)!r} needs directory {str(parent)!r}, which is also an artifact file"
                )
    directories = {parent for destination in seen for parent in destination.parents}
    for destination in sorted(seen):
        for suffix in (".part", ".part.state"):
            working = PurePosixPath(str(destination) + suffix)
            if (working in seen or working in directories
                    or any(parent in seen for parent in working.parents)):
                problems.append(
                    f"artifacts: download working path {str(working)!r} for {str(destination)!r} "
                    "conflicts with an artifact file or directory"
                )

    entry = Entry.from_dict(data)
    if all(a.bytes is not None for a in entry.artifacts):
        try:
            report = staging.compute_staging(entry)
        except staging.StagingError as error:
            problems.append(str(error))
        else:
            problems.extend(staging.check_claim(entry, report))
    else:
        # Sizes are not filled in yet, so only the structure can be checked.
        try:
            _check_staging_references(entry)
        except staging.StagingError as error:
            problems.append(str(error))
    return problems


def _check_staging_references(entry: Entry) -> None:
    for number, step in enumerate(entry.staging, start=1):
        selectors: list[str] = []
        if step.step == "download":
            if step.artifacts not in (None, "all"):
                selectors = list(step.artifacts)  # type: ignore[arg-type]
        else:
            selectors = list(step.inputs or [])
        for selector in selectors:
            if not any(a.path == selector or a.role == selector for a in entry.artifacts):
                raise staging.StagingError(
                    f"staging step {number} ({step.step}): {selector!r} matches no artifact path or role"
                )


@dataclass
class CatalogLoadResult:
    entries: list[Entry] = field(default_factory=list)
    problems: dict[str, list[str]] = field(default_factory=dict)


def load_catalog(directory: str | Path | None = None) -> CatalogLoadResult:
    """Load every entry in a directory.

    A file that fails its checks is reported in `problems` and left out of
    `entries`, so one bad file does not hide the rest.
    """
    path = Path(directory) if directory else default_catalog_dir()
    if path is None or not path.is_dir():
        raise CatalogError(
            "no catalog directory found. Pass --catalog PATH or set ABLIT_CATALOG."
        )
    schema = schema_check.load_schema(schema_path())
    result = CatalogLoadResult()
    for file in sorted(path.glob("*.yaml")):
        try:
            data = load_entry_file(file)
            problems = check_entry(data, schema)
        except CatalogError as error:
            result.problems[file.name] = [str(error)]
            continue
        if not problems and file.stem != data["id"]:
            problems.append(f"the file name must match the entry id {data['id']!r}")
        if problems:
            result.problems[file.name] = problems
            continue
        result.entries.append(Entry.from_dict(data))
    result.entries.sort(key=lambda e: e.id)
    return result


def resolve(entries: list[Entry], model: str, variant: str | None = None) -> Entry:
    """Match a catalog ID or a Hugging Face repo ID to exactly one entry.

    A catalog ID always matches one entry. A repo ID can match several, for
    example two quantizations of one repo, and then `variant` must name one.
    ablit never substitutes one build for another.
    """
    by_id = [e for e in entries if e.id == model]
    if by_id:
        found = by_id[0]
        if variant and variant != found.id:
            raise CatalogError(f"--variant {variant} does not match {found.id}")
        return found

    by_repo = [e for e in entries if e.source.repo.lower() == model.lower()]
    if not by_repo:
        raise ModelNotInCatalog(model)
    if variant:
        picked = [e for e in by_repo if e.id == variant]
        if not picked:
            raise AmbiguousModel(model, [e.id for e in by_repo])
        return picked[0]
    if len(by_repo) > 1:
        raise AmbiguousModel(model, [e.id for e in by_repo])
    return by_repo[0]
