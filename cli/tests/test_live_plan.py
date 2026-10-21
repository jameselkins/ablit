"""The live smoke script's safety rules. These tests never touch the network."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

import live_plan
from market_fixtures import RUNPOD_URL, VAST_URL, FakeRunPod, FakeVast, runpod_type, vast_row


@pytest.fixture(autouse=True)
def _outside_ci(monkeypatch, request):
    """Clear CI variables so each test checks one rule. The CI test sets its own."""
    for name in live_plan.CI_VARIABLES:
        monkeypatch.delenv(name, raising=False)


def test_refuses_to_write_inside_the_repository(repo_root: Path, capsys):
    for target in (repo_root / "out", repo_root / "cli" / "tests" / "out", repo_root):
        assert live_plan.cli([str(target)]) == 2
        assert not (repo_root / "out").exists()
    assert "refusing to write inside the repository" in capsys.readouterr().err


def test_refuses_a_path_that_reaches_into_the_repository_through_dots(repo_root: Path):
    sneaky = repo_root.parent / "elsewhere" / ".." / repo_root.name / "out"
    assert live_plan.cli([str(sneaky)]) == 2


def test_refuses_to_run_in_ci(monkeypatch, tmp_path: Path, capsys):
    monkeypatch.setenv("CI", "true")
    assert live_plan.cli([str(tmp_path / "out")]) == 2
    assert "never run in CI" in capsys.readouterr().err
    assert not (tmp_path / "out").exists()


def test_transport_allows_only_the_two_search_endpoints_and_plain_queries(tmp_path: Path):
    transport = live_plan.ReadOnlyTransport(tmp_path / "requests.log")
    client = httpx.Client(transport=transport)
    for method, url in (("GET", VAST_URL), ("POST", "https://example.com/graphql"), ("POST", "https://console.vast.ai/api/v0/instances/")):
        with pytest.raises(RuntimeError, match="not an allowed search endpoint"):
            client.request(method, url, json={})
    with pytest.raises(RuntimeError, match="not a plain query"):
        client.post(RUNPOD_URL, json={"query": "mutation { podTerminate(input: {podId: \"x\"}) }"})
    with pytest.raises(RuntimeError, match="Authorization"):
        client.post(VAST_URL, json={}, headers={"Authorization": "Bearer invented-test-key"})


def test_a_full_run_writes_one_file_per_entry_and_a_request_log(tmp_path: Path, monkeypatch, repo_root: Path):
    monkeypatch.setattr(live_plan, "MIN_SECONDS_BETWEEN_REQUESTS", 0.0)
    monkeypatch.setenv("VAST_API_KEY", "invented-test-key")  # must not be sent
    seen_headers = []

    def vast(request: httpx.Request):
        seen_headers.append(dict(request.headers))
        return FakeVast([vast_row(9600 + i, base=0.4 + i * 0.1, allocated=600) for i in range(3)])(request)

    router = respx.mock(assert_all_called=False)
    router.post(VAST_URL).mock(side_effect=vast)
    router.post(RUNPOD_URL).mock(side_effect=FakeRunPod([runpod_type("NVIDIA L40S", "L40S", 48, price=0.9)]))
    out = tmp_path / "live"
    with router:
        assert live_plan.cli([str(out)]) == 0
    files = sorted(p.name for p in out.glob("*.json"))
    assert "index.json" in files and "llama31-8b-abliterated-q4km.json" in files
    index = json.loads((out / "index.json").read_text())
    assert len(index) == 5
    plan = json.loads((out / "llama31-8b-abliterated-q4km.json").read_text())
    assert plan["schema"] == 1
    log = [json.loads(line) for line in (out / "requests.log").read_text().splitlines()]
    assert log and all(entry["method"] == "POST" for entry in log)
    assert not any("authorization" in h for h in seen_headers)
