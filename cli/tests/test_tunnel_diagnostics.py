"""Keep SSH failure evidence without putting remote error text in CLI output."""

import contextlib
import stat
from types import SimpleNamespace

import pytest

from ablit.runs import RunError, RunStore
from ablit.runs.ssh import SSH


@pytest.mark.parametrize("exit_code", [None, 255])
def test_tunnel_keeps_a_private_error_log_on_success_and_startup_failure(tmp_path, monkeypatch, exit_code):
    store = RunStore(tmp_path)
    ssh = SSH(store, {"run_id": "diagnostics", "ssh_host": "ssh.example.invalid", "ssh_port": 2222})
    socket = SimpleNamespace(bind=lambda address: None, getsockname=lambda: ("127.0.0.1", 4321))
    monkeypatch.setattr("ablit.runs.ssh.socket.socket", lambda: contextlib.nullcontext(socket))
    monkeypatch.setattr("ablit.runs.ssh.socket.create_connection", lambda *a, **k: contextlib.nullcontext())
    process = SimpleNamespace(poll=lambda: exit_code, terminate=lambda: None, wait=lambda: exit_code)

    def start(args, **kwargs):
        assert "ExitOnForwardFailure=yes" in args
        kwargs["stderr"].write(b"invented SSH failure detail\n")
        return process

    monkeypatch.setattr("ablit.runs.ssh.subprocess.Popen", start)
    if exit_code is None:
        assert ssh.open_tunnel(8080) == (process, 4321)
    else:
        with pytest.raises(RunError, match="exit 255") as error:
            ssh.open_tunnel(8080)
        assert "invented SSH failure detail" not in str(error.value)
    log = ssh.directory / "tunnel.log"
    assert log.read_bytes() == b"invented SSH failure detail\n"
    assert stat.S_IMODE(log.stat().st_mode) == 0o600
