from __future__ import annotations

import hashlib
import json
import re
import shutil
import struct
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from ablit.catalog import gguf
from ablit.catalog.dump import dump_entry, leading_comments
from ablit.notices import SPEND_CAP_NOTICE, spend_cap_notice


# --- GGUF header reader ----------------------------------------------------

def _string(text: str) -> bytes:
    raw = text.encode()
    return struct.pack("<Q", len(raw)) + raw


def _gguf(pairs: list[bytes]) -> bytes:
    return b"GGUF" + struct.pack("<I", 3) + struct.pack("<Q", 0) + struct.pack("<Q", len(pairs)) + b"".join(pairs)


def _kv_string(key: str, value: str) -> bytes:
    return _string(key) + struct.pack("<I", 8) + _string(value)


def test_gguf_architecture_is_read_after_other_keys():
    name = _kv_string("general.name", "A model")
    array = _string("tokenizer.tokens") + struct.pack("<I", 9) + struct.pack("<I", 8) + struct.pack("<Q", 2) + _string("a") + _string("bb")
    count = _string("llama.block_count") + struct.pack("<I", 4) + struct.pack("<I", 32)
    data = _gguf([name, array, count, _kv_string("general.architecture", "llama")])
    assert gguf.read_architecture(data) == "llama"


def test_gguf_truncated_header_asks_for_more_bytes():
    data = _gguf([_kv_string("general.architecture", "llama")])
    with pytest.raises(gguf.NeedMoreBytes):
        gguf.read_architecture(data[:-3])


def test_gguf_reads_architecture_after_deeply_nested_array_metadata():
    nested = struct.pack("<IQ", 9, 1) * (sys.getrecursionlimit() + 10)
    nested += struct.pack("<IQ", 8, 1) + _string("value")
    metadata = _string("custom.nested") + struct.pack("<I", 9) + nested
    assert gguf.read_architecture(_gguf([metadata, _kv_string("general.architecture", "llama")])) == "llama"


def test_gguf_reads_architecture_after_several_nested_arrays():
    arrays = struct.pack("<IQ", 9, 2)
    arrays += struct.pack("<IQ", 8, 2) + _string("a") + _string("bb")
    arrays += struct.pack("<IQII", 4, 2, 17, 42)
    metadata = _string("custom.nested") + struct.pack("<I", 9) + arrays
    assert gguf.read_architecture(_gguf([metadata, _kv_string("general.architecture", "llama")])) == "llama"
    with pytest.raises(gguf.NeedMoreBytes):
        gguf.read_architecture(_gguf([metadata[:-1]]))


def test_gguf_rejects_an_unknown_array_element_type_even_when_empty():
    metadata = _string("custom.invalid") + struct.pack("<IIQ", 9, 99, 0)
    with pytest.raises(gguf.NotGguf, match="unknown value type 99"):
        gguf.read_architecture(_gguf([metadata, _kv_string("general.architecture", "llama")]))


def test_gguf_rejects_other_files():
    with pytest.raises(gguf.NotGguf):
        gguf.read_architecture(b"PK\x03\x04" + b"\0" * 40)
    with pytest.raises(gguf.NotGguf):
        gguf.read_architecture(_gguf([_kv_string("general.name", "x")]))


# --- canonical writer ------------------------------------------------------

def test_dump_round_trips_every_seed_entry(entry_files):
    for path in entry_files:
        text = path.read_text(encoding="utf-8")
        data = yaml.safe_load(text)
        again = dump_entry(data, leading_comments(text))
        assert yaml.safe_load(again) == data
        assert again == text, f"{path.name} is not in the layout that --fill writes"


def test_dump_quotes_strings_that_would_change_type():
    data = {"schema": 1, "id": "x", "display_name": "123", "notes": ["true", "a: b", "plain words, with a comma"]}
    assert yaml.safe_load(dump_entry(data)) == data


@pytest.mark.parametrize("text", [
    "2026-13-01", "2026-02-30", "2026-01-01T00:00:00+25:00", "text\x00value",
    "before\x85after", "before\u2028after", "before\u2029after",
])
@pytest.mark.parametrize("flow", [False, True])
def test_dump_quotes_strings_that_cannot_be_read_as_plain_yaml(text, flow):
    data = {"artifacts": [{"path": text}]} if flow else {"display_name": text}
    assert yaml.safe_load(dump_entry(data)) == data


@pytest.mark.parametrize("codepoint", [0x7F, 0x80, 0x84, 0x86, 0x9F, 0xD800, 0xDFFF, 0xFFFE, 0xFFFF])
@pytest.mark.parametrize("flow", [False, True])
def test_dump_escapes_characters_that_yaml_cannot_read_literally(codepoint, flow):
    value = "before" + chr(codepoint) + "after"
    data = {"artifacts": [{"path": value}]} if flow else {"display_name": value}
    text = dump_entry(data)
    assert yaml.safe_load(text) == data
    assert yaml.safe_load(text.encode("utf-8")) == data


@pytest.mark.parametrize("text", ["A default?", "before?after", "?plain"])
@pytest.mark.parametrize("field", ["sources", "artifacts"])
def test_dump_quotes_question_marks_in_inline_values(text, field):
    key = "note" if field == "sources" else "path"
    data = {field: [{key: text}]}
    assert yaml.safe_load(dump_entry(data)) == data


def test_dump_keeps_ordinary_unicode_text_readable():
    data = {"display_name": "Café: 日本語", "artifacts": [{"path": "日本語, é"}]}
    text = dump_entry(data)
    assert yaml.safe_load(text) == data
    assert "Café: 日本語" in text and "日本語, é" in text


# --- compiled catalog ------------------------------------------------------

def test_build_catalog_is_deterministic_and_complete(repo_root: Path):
    sys.path.insert(0, str(repo_root / "tools"))
    import build_catalog

    first, problems = build_catalog.build(repo_root)
    second, _ = build_catalog.build(repo_root)
    assert not problems
    assert first == second
    document = json.loads(first)
    assert document["catalog_version"] == "2026.10.20.1"
    assert document["schema"] == 1
    assert [e["id"] for e in document["entries"]] == sorted(e["id"] for e in document["entries"])
    assert len(document["entries"]) == 9
    assert hashlib.sha256(first).hexdigest() == hashlib.sha256(second).hexdigest()


def test_build_catalog_rejects_a_bad_version_and_a_bad_entry(tmp_path: Path, repo_root: Path):
    sys.path.insert(0, str(repo_root / "tools"))
    import build_catalog

    shutil.copytree(repo_root / "catalog", tmp_path / "catalog", ignore=shutil.ignore_patterns("dist"))
    (tmp_path / "catalog" / "VERSION").write_text("v1\n")
    (tmp_path / "catalog" / "models" / "broken.yaml").write_text("schema: 1\nid: broken\n")
    _, problems = build_catalog.build(tmp_path)
    assert any("VERSION" in p for p in problems)
    assert any("broken.yaml" in p for p in problems)


@pytest.mark.parametrize("content", [b"schema: [", b"- model", b"\xff", None],
                         ids=["yaml", "mapping", "utf8", "directory"])
def test_build_catalog_reports_unreadable_entries_and_preserves_the_output(
    tmp_path: Path, repo_root: Path, monkeypatch, capsys, content
):
    sys.path.insert(0, str(repo_root / "tools"))
    import build_catalog

    shutil.copytree(repo_root / "catalog", tmp_path / "catalog", ignore=shutil.ignore_patterns("dist"))
    models = tmp_path / "catalog" / "models"
    broken = models / "aaa-broken.yaml"
    if content is None:
        broken.mkdir()
    else:
        broken.write_bytes(content)
    (models / "zzz-invalid.yaml").write_text("schema: 1\nid: zzz-invalid\n", encoding="utf-8")
    out = tmp_path / "catalog" / "dist" / "catalog.json"
    out.parent.mkdir()
    original = b"previous compiled catalog"
    out.write_bytes(original)

    payload, problems = build_catalog.build(tmp_path)
    assert any("aaa-broken.yaml" in problem for problem in problems)
    assert any("zzz-invalid.yaml" in problem for problem in problems)
    assert len(json.loads(payload)["entries"]) == 9

    build = build_catalog.build
    monkeypatch.setattr(build_catalog, "build", lambda: build(tmp_path))
    monkeypatch.setattr(build_catalog, "ROOT", tmp_path)
    assert build_catalog.main() == 1
    captured = capsys.readouterr()
    assert "aaa-broken.yaml" in captured.err and "zzz-invalid.yaml" in captured.err
    assert not captured.out
    assert out.read_bytes() == original


@pytest.mark.parametrize("source_directory", ["missing", "empty"])
def test_build_catalog_requires_entry_files_and_preserves_the_output(
    tmp_path: Path, monkeypatch, capsys, source_directory
):
    import build_catalog

    catalog = tmp_path / "catalog"
    catalog.mkdir()
    (catalog / "VERSION").write_text("2026.10.20.1\n", encoding="utf-8")
    if source_directory == "empty":
        (catalog / "models").mkdir()
    out = catalog / "dist" / "catalog.json"
    out.parent.mkdir()
    original = b"previous compiled catalog"
    out.write_bytes(original)

    build = build_catalog.build
    monkeypatch.setattr(build_catalog, "build", lambda: build(tmp_path))
    monkeypatch.setattr(build_catalog, "ROOT", tmp_path)
    assert build_catalog.main() == 1
    captured = capsys.readouterr()
    assert "catalog/models: no entry files found" in captured.err
    assert not captured.out
    assert out.read_bytes() == original


def test_build_catalog_script_prints_a_digest(repo_root: Path):
    result = subprocess.run([sys.executable, str(repo_root / "tools" / "build_catalog.py")],
                            capture_output=True, text=True, cwd=repo_root)
    assert result.returncode == 0, result.stderr
    assert re.fullmatch(r"[0-9a-f]{64}", result.stdout.strip())
    assert (repo_root / "catalog" / "dist" / "catalog.json").is_file()


def test_catalog_version_file(repo_root: Path):
    assert (repo_root / "catalog" / "VERSION").read_text().strip() == "2026.10.20.1"


# --- notices ---------------------------------------------------------------

def test_cap_notice_fills_in_the_cap():
    assert "$<cap>" in SPEND_CAP_NOTICE
    assert "$12.50" in spend_cap_notice(12.5) and "<cap>" not in spend_cap_notice(12.5)
