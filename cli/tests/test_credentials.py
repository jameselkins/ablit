"""The credential flow uses a fake OS keyring and synthetic HTTP responses."""

import json
import sys

import httpx
import respx

from ablit import credentials as keys
from ablit.backends.vast import VastBackend
from ablit.cli import main
from ablit.runs import RunStore
from test_vast_lifecycle import ROOT, page


def test_keyring_roundtrip_and_environment_priority(isolated_run_state):
    keys.secret_set("accounts/vast", "invented-keyring-key")
    assert keys.credentials("vast", {}).api_key == "invented-keyring-key"
    assert keys.credentials("vast", {"VAST_API_KEY": "invented-env-key"}).api_key == "invented-env-key"
    keys.secret_set("invented-run", "invented-endpoint-key")
    assert keys.secret_get("invented-run") == "invented-endpoint-key"
    keys.secret_delete("invented-run")
    assert keys.secret_get("invented-run") is None
    assert ("ablit", "accounts/vast") in isolated_run_state.values


def test_setup_reads_hidden_key_and_tests_with_one_read_only_call(monkeypatch, capsys):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)
    answers = iter(["yes", "yes"])
    monkeypatch.setattr("builtins.input", lambda prompt: next(answers))
    monkeypatch.setattr(keys.getpass, "getpass", lambda prompt: "invented-setup-key")
    with respx.mock as router:
        route = router.get(ROOT + "/api/v1/instances/").mock(return_value=page([]))
        assert main(["setup", "vast"], environ={}) == 0
        assert route.call_count == 1
        assert route.calls[0].request.method == "GET"
        assert route.calls[0].request.headers["Authorization"] == "Bearer invented-setup-key"
    assert keys.secret_get("accounts/vast") == "invented-setup-key"
    assert "invented-setup-key" not in capsys.readouterr().out


def test_setup_session_choice_never_saves_the_key(monkeypatch):
    answers = iter(["yes", "no"])
    monkeypatch.setattr("builtins.input", lambda prompt: next(answers))
    monkeypatch.setattr(keys.getpass, "getpass", lambda prompt: "invented-session-key")
    with respx.mock as router:
        router.get(ROOT + "/api/v1/instances/").mock(return_value=page([]))
        keys.setup_vast(VastBackend(httpx.Client()), RunStore(), False, {})
    assert keys.secret_get("accounts/vast") is None
    assert keys.SESSION["vast"] == "invented-session-key"


def test_no_account_has_only_direct_signup_and_skip(monkeypatch, capsys):
    answers = iter(["no", "2"])
    monkeypatch.setattr("builtins.input", lambda prompt: next(answers))
    with respx.mock:
        assert keys.setup_vast(VastBackend(httpx.Client()), RunStore(), True, {}) == 0
    out = capsys.readouterr().out
    assert "Sign up directly" in out and "Skip" in out and "paid link" not in out
    assert "https://cloud.vast.ai/" in out
    assert json.loads((RunStore().root / "config.json").read_text())["referral"] is False
