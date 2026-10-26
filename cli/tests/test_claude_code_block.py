"""The Claude Code block that `ablit up` prints, and `status --key-only` that feeds it."""

import pytest
import respx
import yaml

from ablit import credentials as keys
from ablit.catalog.loader import repo_root
from ablit.cli import main
from ablit.runs import RunStore
from ablit.runs import monitor as monitor_mod
from test_run_commands import MODEL, saved_run


def test_key_only_prints_just_the_key_and_contacts_no_marketplace(capsys):
    r = saved_run(RunStore())
    keys.secret_set(r["run_id"], "invented-endpoint-key")
    # No marketplace key is set, and respx refuses every request.
    with respx.mock:
        assert main(["status", r["run_id"], "--key-only"], environ={}) == 0
    assert capsys.readouterr().out == "invented-endpoint-key\n"


@pytest.mark.parametrize("flag", ["--json", "--watch", "--all", "--show-key"])
def test_key_only_refuses_other_status_flags(capsys, flag):
    r = saved_run(RunStore())
    keys.secret_set(r["run_id"], "invented-endpoint-key")
    assert main(["status", r["run_id"], "--key-only", flag], environ={}) == 2
    captured = capsys.readouterr()
    assert "--key-only" in captured.err and "invented-endpoint-key" not in captured.out


def test_key_only_exits_4_when_the_key_is_gone(capsys):
    r = saved_run(RunStore())
    assert main(["status", r["run_id"], "--key-only"], environ={}) == 4
    assert capsys.readouterr().out == ""


def test_endpoint_prints_a_claude_code_block_without_the_key(capsys):
    r = saved_run(RunStore())
    assert r["anthropic_messages"] is True
    keys.secret_set(r["run_id"], "invented-endpoint-key")
    monitor_mod.endpoint({**r, "local_port": 18001})
    out = capsys.readouterr().out
    assert "invented-endpoint-key" not in out
    key_command = f'"$(ablit status {r["run_id"]} --key-only)"'
    assert f"export ABLIT_KEY={key_command}\n" in out
    assert f"export ANTHROPIC_AUTH_TOKEN={key_command}\n" in out
    # Claude Code adds /v1/messages itself, so the base URL has no /v1.
    assert "export ANTHROPIC_BASE_URL=http://127.0.0.1:18001\n" in out
    for name in ("ANTHROPIC_MODEL", "ANTHROPIC_DEFAULT_HAIKU_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL",
                 "ANTHROPIC_DEFAULT_OPUS_MODEL", "ANTHROPIC_DEFAULT_FABLE_MODEL", "CLAUDE_CODE_SUBAGENT_MODEL"):
        assert f"export {name}={MODEL}\n" in out
    assert "export CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1\n" in out
    # The llama.cpp entry serves 4096 tokens, which Claude Code's system prompt overflows.
    assert "serves 4096 tokens of context" in out
    assert out.rstrip().endswith(f"Run `ablit down {r['run_id']}` to stop it.")


def test_endpoint_skips_the_claude_code_block_for_engines_without_messages(capsys):
    r = saved_run(RunStore())
    older = {k: v for k, v in r.items() if k != "anthropic_messages"}
    for record in ({**r, "anthropic_messages": False}, older):
        monitor_mod.endpoint({**record, "local_port": 18001})
        out = capsys.readouterr().out
        assert "Endpoint: http://127.0.0.1:18001/v1" in out
        assert "ANTHROPIC" not in out and "Claude Code" not in out


def test_no_context_warning_when_the_entry_fits_claude_code():
    r = saved_run(RunStore())
    r["entry"]["profile"]["context_tokens"] = 32768
    block = monitor_mod.claude_code_block({**r, "local_port": 18001})
    assert "tokens of context" not in block


@pytest.mark.parametrize(("engine", "expected"), [("llamacpp", True), ("xolotl", True), ("vllm", False)])
def test_engine_locks_say_whether_they_answer_messages(engine, expected):
    root = repo_root()
    assert root is not None
    lock = yaml.safe_load((root / "engines" / engine / "lock.yaml").read_text())
    assert lock["anthropic_messages"] is expected
