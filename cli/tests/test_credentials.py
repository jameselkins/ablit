"""The credential flow uses a fake OS keyring and synthetic HTTP responses."""

import json
import sys
from pathlib import Path
from urllib.parse import quote

import httpx
import pytest
import respx

from ablit import credentials as keys
from ablit.backends.vast import VastBackend
from ablit.backends.errors import CredentialsRejected
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


@pytest.mark.parametrize("content", [b"{", b"[]", b"null", b"\xff"])
@pytest.mark.parametrize("no_referral", [False, True])
def test_setup_refuses_invalid_config_before_prompts_and_preserves_it(
    monkeypatch, capsys, isolated_run_state, content, no_referral,
):
    config = RunStore().root / "config.json"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_bytes(content)
    session_before = keys.SESSION.copy()

    def unexpected(*args, **kwargs):
        pytest.fail("An invalid setup configuration must not prompt or test a key.")

    class Driver:
        test_credentials = staticmethod(unexpected)

    monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr("builtins.input", unexpected)
    monkeypatch.setattr(keys.getpass, "getpass", unexpected)
    args = ["setup", "vast"] + (["--no-referral"] if no_referral else [])
    assert main(args, backends={"vast": Driver()}, environ={}) == 2
    output = capsys.readouterr()
    assert "setup configuration" in output.err and str(config) in output.err
    assert output.out == ""
    assert config.read_bytes() == content
    assert keys.SESSION == session_before and isolated_run_state.values == {}


@pytest.mark.parametrize("backend", ["vast", "runpod", "clore", "lium"])
def test_setup_reports_unreadable_config_before_prompts(monkeypatch, capsys, backend):
    config = RunStore().root / "config.json"
    config.parent.mkdir(parents=True, exist_ok=True)
    content = b'{"referral": false}'
    config.write_bytes(content)
    read_text = Path.read_text

    def denied(path, *args, **kwargs):
        if path == config:
            raise PermissionError("invented configuration read denial")
        return read_text(path, *args, **kwargs)

    def unexpected(*args, **kwargs):
        pytest.fail("An unreadable setup configuration must not prompt or test a key.")

    class Driver:
        test_credentials = staticmethod(unexpected)

    monkeypatch.setattr(Path, "read_text", denied)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr("builtins.input", unexpected)
    monkeypatch.setattr(keys.getpass, "getpass", unexpected)
    assert main(["setup", backend], backends={backend: Driver()}, environ={}) == 2
    output = capsys.readouterr()
    assert "setup configuration" in output.err and str(config) in output.err
    assert "invented configuration read denial" in output.err and output.out == ""
    assert config.read_bytes() == content


@pytest.mark.parametrize("backend", ["vast", "runpod", "clore", "lium"])
@pytest.mark.parametrize("failure", ["rejected", "network", "unexpected"])
def test_setup_failure_diagnostics_redact_the_entered_key(monkeypatch, capsys, backend, failure):
    key = 'invented/Setup+"Key'
    forms = [key, quote(key, safe=""), json.dumps(key)[1:-1], key[:8] + "\x1b" + key[8:]]
    calls = []

    class Driver:
        def test_credentials(self, creds):
            calls.append(creds.api_key)
            detail = "invented test failure: " + " ".join(forms)
            if failure == "rejected":
                raise CredentialsRejected(backend, 401, detail)
            raise (httpx.ConnectError(detail) if failure == "network" else ValueError(detail))

    monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)
    answers = iter(["yes"])
    monkeypatch.setattr("builtins.input", lambda prompt: next(answers))
    monkeypatch.setattr(keys.getpass, "getpass", lambda prompt: key)
    assert main(["setup", backend], backends={backend: Driver()}, environ={}) == 4
    captured = capsys.readouterr()
    output = captured.out + captured.err
    assert all(form not in output for form in forms)
    assert output.count("[redacted]") == 4
    assert "invented test failure" in output and "\x1b" not in output
    if failure == "rejected":
        assert f"{backend} answered with HTTP 401" in output
    assert calls == [key]
    assert keys.secret_get("accounts/" + backend) is None and backend not in keys.SESSION


@pytest.mark.parametrize("rejected", [False, True])
def test_setup_does_not_echo_a_short_key_in_failure_details(monkeypatch, capsys, rejected):
    key = "tiny"

    class Driver:
        def test_credentials(self, creds):
            if rejected:
                raise CredentialsRejected("vast", 403, "refused " + key)
            raise ValueError("cannot use " + key)

    monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr("builtins.input", lambda prompt: "yes")
    monkeypatch.setattr(keys.getpass, "getpass", lambda prompt: key)
    assert main(["setup", "vast"], backends={"vast": Driver()}, environ={}) == 4
    captured = capsys.readouterr()
    assert key not in captured.out + captured.err
    assert keys.secret_get("accounts/vast") is None


@pytest.mark.parametrize("rejected", [False, True])
def test_setup_errors_suppress_original_secret_bearing_tracebacks(monkeypatch, rejected):
    import traceback

    key = "invented-private-setup-key"
    detail = "cannot test " + key

    class Driver:
        def test_credentials(self, creds):
            if rejected:
                raise CredentialsRejected("vast", 403, detail)
            raise ValueError(detail)

    monkeypatch.setattr("builtins.input", lambda prompt: "yes")
    monkeypatch.setattr(keys.getpass, "getpass", lambda prompt: key)
    with pytest.raises(CredentialsRejected if rejected else keys.CredentialError) as caught:
        keys.setup_vast(Driver(), RunStore(), False, {})
    assert key not in "".join(traceback.format_exception(caught.value))
    assert "[redacted]" in str(caught.value)
    if rejected:
        assert (caught.value.backend, caught.value.status) == ("vast", 403)
        assert key not in caught.value.detail


def test_no_account_has_only_direct_signup_and_skip(monkeypatch, capsys):
    answers = iter(["no", "2"])
    monkeypatch.setattr("builtins.input", lambda prompt: next(answers))
    with respx.mock:
        assert keys.setup_vast(VastBackend(httpx.Client()), RunStore(), True, {}) == 0
    out = capsys.readouterr().out
    assert "Sign up directly" in out and "Skip" in out and "paid link" not in out
    assert "https://cloud.vast.ai/" in out
    assert json.loads((RunStore().root / "config.json").read_text())["referral"] is False
