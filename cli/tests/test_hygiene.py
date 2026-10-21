"""Checks that the public repository stands alone and follows the writing rules."""

from __future__ import annotations

import re
from pathlib import Path


SKIP_DIRS = {".git", ".venv", ".hypothesis", ".pytest_cache", "__pycache__", "dist", "node_modules"}
LICENSE_FILES = {"LICENSE"}
VERBATIM: set[str] = set()

FORBIDDEN_WORDS = [
    r"load-bearing", r"load bearing", r"\bCodex\b", r"\borchestrator\b", r"\bPhase\b",
    r"\bA-0\d\d\b", r"\bC-0\d\d\b", r"\bR(?:[1-9]|10)\b", r"ablit-planning", r"ASSUMPTIONS", r"FINAL-PLAN",
]
PERSONAL = [r"@[A-Za-z0-9-]+\.(?:com|org|net|io)\b", r"/Users/", r"\bjameselkins@", r"sk-ant-", r"hf_[A-Za-z0-9]{20,}"]


def _files(root: Path):
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root)
        if not path.is_file() or any(part in SKIP_DIRS for part in rel.parts):
            continue
        try:
            yield rel, path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue


def test_no_planning_references_or_forbidden_words(repo_root: Path):
    hits = []
    for rel, text in _files(repo_root):
        if rel.name in LICENSE_FILES or rel.name in VERBATIM or rel.parts[:2] == ("cli", "tests"):
            continue
        for pattern in FORBIDDEN_WORDS:
            for match in re.finditer(pattern, text):
                hits.append(f"{rel}: {match.group(0)}")
    assert not hits, hits


def test_no_personal_data_or_secrets(repo_root: Path):
    hits = []
    for rel, text in _files(repo_root):
        if rel.name in LICENSE_FILES or rel.parts[:2] == ("cli", "tests"):
            continue
        for pattern in PERSONAL:
            for match in re.finditer(pattern, text):
                hits.append(f"{rel}: {match.group(0)}")
    assert not hits, hits


def test_notice_names_only_the_maintainer(repo_root: Path):
    assert (repo_root / "NOTICE").read_text(encoding="utf-8").strip() == "ablit, Copyright 2026 James Elkins"


def test_prose_has_no_em_or_en_dashes(repo_root: Path):
    hits = []
    for rel, text in _files(repo_root):
        if rel.name in LICENSE_FILES or rel.name in VERBATIM or rel.parts[:2] == ("cli", "tests"):
            continue
        if "—" in text or "–" in text:
            hits.append(str(rel))
    assert not hits, hits


def test_prose_uses_straight_quotes(repo_root: Path):
    hits = []
    for rel, text in _files(repo_root):
        if rel.name in LICENSE_FILES or rel.name in VERBATIM or rel.parts[:2] == ("cli", "tests"):
            continue
        if re.search("[“”‘’]", text):
            hits.append(str(rel))
    assert not hits, hits


def test_license_files_are_unmodified_standard_texts(repo_root: Path):
    apache = (repo_root / "LICENSE").read_text(encoding="utf-8")
    assert apache.lstrip().startswith("Apache License")
    assert "Version 2.0, January 2004" in apache
    assert "END OF TERMS AND CONDITIONS" in apache
    assert len(apache.encode()) == 11358
    cc0 = (repo_root / "catalog" / "LICENSE").read_text(encoding="utf-8")
    assert "CC0 1.0 Universal" in cc0 and "Creative Commons Legal Code" in cc0


def test_documentation_files_exist(repo_root: Path):
    for name in ("README.md", "LICENSE", "NOTICE", "POLICY.md", "SECURITY.md", "CONTRIBUTING.md", "BUILD.md",
                 ".gitignore", "catalog/VERSION", "catalog/LICENSE", "catalog/schema/v1.json",
                 "tools/verify_entry.py", "tools/build_catalog.py",
                 "backends/vast.md", "backends/runpod.md", "backends/clore.md",
                 ".github/workflows/ci.yml", ".github/workflows/weekly-catalog.yml",
                 ".github/workflows/release.yml", "pyproject.toml", ".importlinter"):
        assert (repo_root / name).is_file(), name
    for engine in ("vllm", "llamacpp", "xolotl"):
        for name in ("bootstrap.sh", "health.sh", "lock.yaml"):
            assert (repo_root / "engines" / engine / name).is_file(), f"{engine}/{name}"


def test_workflows_pin_actions_to_full_commit_shas_and_never_use_secrets(repo_root: Path):
    for name in ("ci.yml", "weekly-catalog.yml"):
        text = (repo_root / ".github" / "workflows" / name).read_text(encoding="utf-8")
        uses = re.findall(r"^\s*-?\s*uses:\s*(\S+)", text, re.M)
        assert uses, name
        for ref in uses:
            assert re.fullmatch(r"[\w./-]+@[0-9a-f]{40}", ref), f"{name}: {ref} is not pinned to a full commit SHA"
        assert not re.search(r"\$\{\{\s*secrets", text)
        assert "pull_request_target" not in text
        assert re.search(r"permissions:\s*\n\s+contents:\s*read", text), name


def test_engine_scripts_follow_the_rules(repo_root: Path):
    for engine in ("vllm", "llamacpp", "xolotl"):
        for script in ("bootstrap.sh", "health.sh"):
            text = (repo_root / "engines" / engine / script).read_text(encoding="utf-8")
            assert text.startswith("#!/usr/bin/env bash")
            assert "set -euo pipefail" in text
            assert "0.0.0.0" not in text
        text = (repo_root / "engines" / engine / "bootstrap.sh").read_text(encoding="utf-8")
        assert ("untested on a rented machine" if engine == "llamacpp" else "TODO(step") in text
        lock = (repo_root / "engines" / engine / "lock.yaml").read_text(encoding="utf-8")
        if engine == "llamacpp":
            assert re.search(r"^digest: sha256:[a-f0-9]{64}$", lock, re.M)
        else:
            assert re.search(r"^digest: null", lock, re.M)
            assert "digest must be pinned before first" in lock
        assert "127.0.0.1" in lock
    common = (repo_root / "engines" / "lib" / "common.sh").read_text(encoding="utf-8")
    for stage in ("01", "02", "03", "04", "05", "06", "07", "08", "09"):
        assert re.search(rf"\b{stage}\b", " ".join(
            (repo_root / "engines" / e / "bootstrap.sh").read_text(encoding="utf-8") for e in ("vllm", "llamacpp", "xolotl")))
    assert "127.0.0.1" in common
    assert 'ABLIT_ENDPOINT_KEY' in common


def test_engine_key_is_never_placed_on_a_command_line(repo_root: Path):
    for path in (repo_root / "engines").rglob("*.sh"):
        text = path.read_text(encoding="utf-8")
        assert "--api-key" not in text, path
        assert not re.search(r"echo\s+.*ENDPOINT_KEY", text), path
        assert not re.search(r"printf\s+.*ENDPOINT_KEY.*>", text), path


def test_backend_notes_exist_and_describe_the_requests(repo_root: Path):
    vast = (repo_root / "backends" / "vast.md").read_text(encoding="utf-8")
    assert "console.vast.ai/api/v0/bundles/" in vast and "dph_total" in vast and "allocated_storage" in vast
    runpod = (repo_root / "backends" / "runpod.md").read_text(encoding="utf-8")
    assert "podFindAndDeployOnDemand" in runpod and "terminateAfter" in runpod and "uninterruptablePrice" in runpod
    clore = (repo_root / "backends" / "clore.md").read_text(encoding="utf-8")
    assert "not in v1" in clore.lower() and "per minute" in clore.lower() and "per day" in clore.lower()


def test_readme_covers_the_required_sections(repo_root: Path):
    readme = (repo_root / "README.md").read_text(encoding="utf-8")
    for needle in ("alpha", "Paid links", "checked 2026-10-20", "llama31-8b-abliterated-q4km",
                   "--endpoint", "127.0.0.1"):
        assert needle in readme, needle
    assert "no referral ids are configured yet" in readme.lower()
