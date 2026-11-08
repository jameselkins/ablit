"""Command tests never use a marketplace account or a real SSH host."""

import contextlib
import io
import json
import sys
from types import SimpleNamespace

import httpx
import pytest
import respx

from ablit import credentials as keys
from ablit.backends.vast import VastBackend
from ablit.catalog import load_catalog, resolve
from ablit.cli import main
from ablit.runs import RunError, RunStore
from ablit.runs import down as down_mod, monitor as monitor_mod, up as up_mod
from ablit.runs.ssh import SSH
from market_fixtures import FakeVast, vast_row, VAST_URL
from test_vast_lifecycle import ROOT, page, instance

MODEL = "llama31-8b-abliterated-q4km"


def tty(monkeypatch):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True, raising=False)


def launch_environment(monkeypatch):
    tty(monkeypatch)
    monkeypatch.setattr(up_mod.engines, "check_artifacts", lambda entry: None)
    monkeypatch.setattr(SSH, "identity_public", lambda self: "ssh-ed25519 invented-public-content")
    keys.secret_set("accounts/vast", "invented-account-key")


def launch_routes(router):
    router.post(VAST_URL).mock(side_effect=FakeVast([vast_row(7101, host_id=7301)]))
    router.post(ROOT + "/api/v0/ssh/").respond(200, json={"success": True, "key": {"id": 7501}})
    create = router.put(ROOT + "/api/v0/asks/7101/").respond(200, json={"success": True, "new_contract": 7201})
    router.get(ROOT + "/api/v1/instances/").mock(return_value=page([instance()]))
    scoped = router.post(ROOT + "/api/v0/auth/apikeys/").respond(200, json={"id": 7401, "key": "invented-scoped-token"})
    return create, scoped


def test_rent_is_required_and_cancel_sends_no_mutations(monkeypatch, capsys):
    launch_environment(monkeypatch)
    monkeypatch.setattr("builtins.input", lambda prompt: "yes")
    with respx.mock as router:
        router.post(VAST_URL).mock(side_effect=FakeVast([vast_row(7101)]))
        assert main(["up", MODEL, "--untested", "--max-spend", "5"], environ={}) == 2
        assert all(call.request.method == "POST" and call.request.url.path == "/api/v0/bundles/" for call in router.calls)
    assert "Rental cancelled" in capsys.readouterr().out


def test_too_small_cap_exits_5_with_a_minimum_and_no_prompt(monkeypatch, capsys):
    launch_environment(monkeypatch)
    monkeypatch.setattr("builtins.input", lambda prompt: pytest.fail("cannot ask rent at a rejected cap"))
    with respx.mock as router:
        router.post(VAST_URL).mock(side_effect=FakeVast([vast_row(7101)]))
        assert main(["up", MODEL, "--untested", "--max-spend", "0.00001"], environ={}) == 5
    assert "Smallest cap" in capsys.readouterr().out
    assert RunStore().all() == []


def test_null_digest_is_refused_before_search(monkeypatch):
    launch_environment(monkeypatch)
    monkeypatch.setattr(up_mod.engines, "engine_lock", lambda e: {"digest": None})
    with respx.mock:
        assert main(["up", MODEL, "--untested", "--max-spend", "5"], environ={}) == 5


XOLOTL = "ds41-1017-xq-v3-uncensored"
UNVERIFIED = "llama33-70b-fp8-dynamic"  # the status check runs before the engine check


def xolotl_launch(monkeypatch, rows, budget="50", extra=()):
    """Run `up` for a Xolotl entry against invented offers and return the exit code and the create calls."""
    launch_environment(monkeypatch)
    monkeypatch.setattr("builtins.input", lambda prompt: "rent")
    monkeypatch.setattr(up_mod, "monitor", lambda *args, **kwargs: 0)
    with respx.mock(assert_all_called=False) as router:
        create, scoped = launch_routes(router)
        fake = FakeVast(rows)
        router.post(VAST_URL).mock(side_effect=fake)
        router.get(ROOT + "/api/v1/instances/").mock(
            side_effect=lambda request: page([instance(label="ablit-" + RunStore().all()[0]["run_id"])]))
        code = main(["up", XOLOTL, "--untested", "--max-spend", budget, *extra], environ={})
        return code, create, fake


def test_up_accepts_a_xolotl_entry_and_rents_with_the_pinned_image_and_enough_disk(monkeypatch):
    code, create, fake = xolotl_launch(monkeypatch, [vast_row(7101, host_id=7301, cuda_max_good=13.0)])
    assert code == 0 and create.call_count == 1
    body = json.loads(create.calls[0].request.content)
    lock = up_mod.engines.engine_lock(resolve(load_catalog().entries, XOLOTL))
    assert body["image"] == f"{lock['image']}@{lock['digest']}"
    # The staging peak, the image, the join margin and the room for the fallback extraction all have to fit.
    assert body["disk"] >= 604 and body["runtype"] == "ssh"
    search = fake.bodies[0]
    assert search["cuda_max_good"] == {"gte": 13.0}
    assert search["compute_cap"] == {"gte": 860}
    assert search["inet_down"] == {"gte": 1000} and search["cpu_ram"] == {"gte": 90000}
    record = RunStore().all()[0]
    assert record["entry"]["id"] == XOLOTL and record["image"].endswith(lock["digest"])


def test_up_refuses_a_xolotl_lock_without_a_pinned_digest(monkeypatch, capsys):
    launch_environment(monkeypatch)
    real = up_mod.engines.engine_lock
    monkeypatch.setattr(up_mod.engines, "engine_lock", lambda e: {**real(e), "digest": None})
    monkeypatch.setattr("builtins.input", lambda prompt: pytest.fail("cannot ask rent without a digest"))
    with respx.mock(assert_all_called=False) as router:
        router.post(VAST_URL).mock(side_effect=lambda request: pytest.fail("no search without a digest"))
        assert main(["up", XOLOTL, "--untested", "--max-spend", "50"], environ={}) == 5
    assert "no valid pinned digest" in capsys.readouterr().out
    assert RunStore().all() == []


def test_up_still_applies_the_cap_check_and_yes_to_a_xolotl_entry(monkeypatch, capsys):
    code, create, fake = xolotl_launch(monkeypatch, [vast_row(7101, host_id=7301)], budget="0.01")
    assert code == 5 and create.call_count == 0
    assert "Smallest cap" in capsys.readouterr().out
    assert RunStore().all() == []


def test_up_refuses_an_unverified_entry_without_untested(monkeypatch, capsys):
    launch_environment(monkeypatch)
    with respx.mock(assert_all_called=False) as router:
        router.post(VAST_URL).mock(side_effect=lambda request: pytest.fail("no search without --untested"))
        assert main(["up", UNVERIFIED, "--max-spend", "50"], environ={}) == 2
    assert "Pass --untested to run it anyway." in capsys.readouterr().err
    assert RunStore().all() == []


def test_up_rejects_a_host_with_a_gpu_generation_the_image_does_not_hold(monkeypatch, capsys):
    # Compute capability 8.0 is an A100, which is not in the image build.
    code, create, fake = xolotl_launch(monkeypatch, [vast_row(7101, host_id=7301, compute_cap=800)])
    assert code == 3 and create.call_count == 0
    assert "GPU generation is not in the engine image" in capsys.readouterr().out


def test_up_still_refuses_an_engine_that_cannot_launch_yet(monkeypatch):
    launch_environment(monkeypatch)
    with respx.mock(assert_all_called=False) as router:
        router.post(VAST_URL).mock(side_effect=lambda request: pytest.fail("no search for an engine that cannot launch"))
        with pytest.raises(up_mod.RunError, match="llama.cpp and Xolotl entries on Vast"):
            up_mod._up(SimpleNamespace(endpoint="tunnel", backends=None),
                         resolve(load_catalog().entries, "ds41-1017-abl-fp8"), None, None, {}, None,
                         None, set(), contextlib.ExitStack())


def test_progress_lines_report_each_ten_percent_once():
    shown = {}
    record = {"progress": {"download": {"bytes_done": 55e9, "bytes_total": 100e9, "mbps": 940.4},
                           "join": {"bytes_done": 0, "bytes_total": 0}}}
    first = monitor_mod.progress_lines(record, shown)
    assert first == ["download 50 percent, 55.0 of 100.0 GB, 940 Mbps"]
    assert monitor_mod.progress_lines(record, shown) == []
    record["progress"]["download"]["bytes_done"] = 100e9
    assert monitor_mod.progress_lines(record, shown) == ["download 100 percent, 100.0 of 100.0 GB, 940 Mbps"]


def test_typed_rent_creates_and_saves_only_nonsecret_records(monkeypatch):
    launch_environment(monkeypatch)
    monkeypatch.setattr("builtins.input", lambda prompt: "rent")
    monkeypatch.setattr(up_mod, "monitor", lambda *args, **kwargs: 0)
    with respx.mock(assert_all_called=False) as router:
        create, scoped = launch_routes(router)
        # Reconciliation uses the generated run label, rather than a fixture ID.
        def listing(request):
            record = RunStore().all()[0]
            return page([instance(label="ablit-" + record["run_id"])])
        router.get(ROOT + "/api/v1/instances/").mock(side_effect=listing)
        assert main(["up", MODEL, "--untested", "--max-spend", "5"], environ={}) == 0
        record = RunStore().all()[0]
        assert create.call_count == scoped.call_count == 1
    assert record["resource_ids"] == ["7201"]
    key = keys.secret_get(record["run_id"])
    assert len(bytes.fromhex(key)) == 32
    raw = RunStore().path(record["run_id"]).read_text()
    assert all(secret not in raw for secret in (key, "invented-account-key", "invented-scoped-token"))
    assert record["create_sent"] and record["watchdog_key_id"] == "7401"


@pytest.mark.parametrize("error_type", [TypeError, OSError, RunError, ValueError, httpx.HTTPError])
@pytest.mark.parametrize("cleanup_code", [0, 7])
def test_startup_error_cleanup_preserves_other_runs_and_redacts_diagnostics(monkeypatch, capsys, error_type, cleanup_code):
    launch_environment(monkeypatch)
    store = RunStore()
    existing = saved_run(store)
    existing.update(state="running", resource_ids=["7199"])
    store.save(existing)
    monkeypatch.setattr("builtins.input", lambda prompt: "rent")
    cleaned = []

    def failed_prepare(store, record, entry, driver, creds):
        assert record["resource_ids"] == ["7201"]
        keys.secret_set(record["run_id"], "invented-startup-endpoint-key")
        keys.secret_set("watchdog/" + record["run_id"], "invented-startup-watchdog-key")
        raise error_type("startup reply containing invented-account-key, invented-\x1baccount-key, "
                         "invented-startup-endpoint-key and invented-startup-watchdog-key")

    def cleanup(store, record, driver, creds):
        cleaned.append(record["run_id"])
        record["state"] = "termination_requested" if cleanup_code else "billing_stopped_confirmed"
        store.save(record)
        return cleanup_code

    monkeypatch.setattr(up_mod, "prepare", failed_prepare)
    monkeypatch.setattr(up_mod, "down", cleanup)
    with respx.mock(assert_all_called=False) as router:
        create, _ = launch_routes(router)
        code = main(["up", MODEL, "--untested", "--max-spend", "5", "--another", "--backends", "vast"], environ={})
        assert code == (cleanup_code or 6) and create.call_count == 1
    new_run = next(record for record in store.all() if record["run_id"] != existing["run_id"])
    assert cleaned == [new_run["run_id"]]
    assert store.load(existing["run_id"]) == existing
    captured = capsys.readouterr()
    for secret in ("invented-account-key", "invented-startup-endpoint-key", "invented-startup-watchdog-key"):
        assert secret not in captured.out + captured.err
    assert "\x1b" not in captured.err
    if error_type in (RunError, ValueError, httpx.HTTPError):
        assert "startup reply containing" in captured.err and "[redacted]" in captured.err


def test_yes_rents_without_a_terminal_or_a_prompt(monkeypatch, capsys):
    launch_environment(monkeypatch)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False, raising=False)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False, raising=False)
    monkeypatch.setattr("builtins.input", lambda prompt: pytest.fail("--yes must not prompt"))
    monkeypatch.setattr(up_mod, "monitor", lambda *args, **kwargs: 0)
    with respx.mock(assert_all_called=False) as router:
        create, scoped = launch_routes(router)
        def listing(request):
            return page([instance(label="ablit-" + RunStore().all()[0]["run_id"])])
        router.get(ROOT + "/api/v1/instances/").mock(side_effect=listing)
        assert main(["up", MODEL, "--untested", "--max-spend", "5", "--yes"], environ={}) == 0
        assert create.call_count == 1
    assert "Quote approved with --yes." in capsys.readouterr().out


def test_yes_still_refuses_a_cap_too_small_for_setup(monkeypatch):
    launch_environment(monkeypatch)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False, raising=False)
    with respx.mock as router:
        router.post(VAST_URL).mock(side_effect=FakeVast([vast_row(7101)]))
        assert main(["up", MODEL, "--untested", "--max-spend", "0.00001", "--yes"], environ={}) == 5
    assert RunStore().all() == []


def test_second_run_requires_another_and_creating_run_recovers(monkeypatch):
    launch_environment(monkeypatch)
    monkeypatch.setattr("builtins.input", lambda prompt: "rent")
    monkeypatch.setattr(up_mod, "monitor", lambda *args, **kwargs: 0)
    with respx.mock(assert_all_called=False) as router:
        create, scoped = launch_routes(router)
        def listing(request):
            return page([instance(label="ablit-" + r["run_id"], instance_id=int(r["resource_ids"][0]) if r["resource_ids"] else 7201 + i)
                         for i, r in enumerate(RunStore().all())])
        router.get(ROOT + "/api/v1/instances/").mock(side_effect=listing)
        assert main(["up", MODEL, "--untested", "--max-spend", "5"], environ={}) == 0
        # A CLI restart resumes a creating record and never creates again.
        assert main(["up", MODEL, "--untested", "--max-spend", "5"], environ={}) == 0
        assert create.call_count == 1
        store = RunStore()
        r = store.all()[0]
        r["state"] = "running"
        store.save(r)
        assert main(["up", MODEL, "--untested", "--max-spend", "5"], environ={}) == 2
        assert create.call_count == 1
        assert main(["up", MODEL, "--untested", "--max-spend", "5", "--another"], environ={}) == 0
        assert create.call_count == 2


def test_price_change_cancels_confirmation(monkeypatch):
    launch_environment(monkeypatch)
    monkeypatch.setattr("builtins.input", lambda prompt: "rent")
    with respx.mock as router:
        router.post(VAST_URL).mock(side_effect=[
            httpx.Response(200, json={"offers": [vast_row(7101)]}),
            httpx.Response(200, json={"offers": [vast_row(7101, base=0.9)]})])
        assert main(["up", MODEL, "--untested", "--max-spend", "5"], environ={}) == 5
    assert not RunStore().all()


@pytest.mark.parametrize("version", [12.4, None, "missing"])
def test_changed_vast_cuda_support_cancels_before_any_account_mutation(monkeypatch, capsys, version):
    launch_environment(monkeypatch)
    monkeypatch.setattr("builtins.input", lambda prompt: "rent")
    monkeypatch.setattr(up_mod, "monitor", lambda *args, **kwargs: 0)
    changed = vast_row(7101, host_id=7301, cuda_max_good=version)
    if version == "missing":
        del changed["cuda_max_good"]
    with respx.mock(assert_all_called=False) as router:
        create, scoped = launch_routes(router)
        router.post(VAST_URL).mock(side_effect=[
            httpx.Response(200, json={"offers": [vast_row(7101, host_id=7301, cuda_max_good=12.8)]}),
            httpx.Response(200, json={"offers": [changed]}),
        ])
        code = main(["up", MODEL, "--untested", "--max-spend", "5", "--backends", "vast"], environ={})
        assert code == 5 and create.call_count == 0 and scoped.call_count == 0
        assert all(call.request.url.path == "/api/v0/bundles/" for call in router.calls)
    assert RunStore().all() == []
    assert "offer or price changed" in capsys.readouterr().out


def test_missing_or_rejected_credentials_exit_4(monkeypatch):
    tty(monkeypatch)
    with respx.mock:
        assert main(["up", MODEL, "--untested", "--max-spend", "5"], environ={}) == 4
    keys.secret_set("accounts/vast", "invented-bad-key")
    with respx.mock as router:
        router.post(VAST_URL).respond(401, json={"success": False})
        assert main(["up", MODEL, "--untested", "--max-spend", "5"], environ={}) == 4


def saved_run(store):
    from ablit.select.request import request_from_entry
    from ablit.select.types import PlanOptions
    entry = resolve(load_catalog().entries, MODEL)
    offer = __import__("test_vast_lifecycle").OFFER
    lock = up_mod.engines.engine_lock(entry)
    from ablit.plan import PlanReport
    req = request_from_entry(entry, lock["image_gb"])
    from ablit.select import rank_offers
    report = PlanReport(entry, req, PlanOptions(), 5, rank_offers([offer], req, PlanOptions()).ranked,
                        [], [], 1, True, "invented-check", lock["digest"])
    data = up_mod.make_intent(store, entry, offer, report, SimpleNamespace(max_spend=5, min_download=None), lock)
    data.update(state="running", resource_ids=["7201"], ssh_host="ssh.example.invalid", ssh_port=2201,
                watchdog_key_id="7401", watchdog_key_sent=True, create_sent=True)
    store.save(data)
    return data


@pytest.mark.parametrize(("error_type", "code"), [(keys.CredentialError, 4), (RunError, 2), (RuntimeError, 7)])
def test_cli_errors_remove_terminal_controls_and_keep_the_exit_code(monkeypatch, capsys, error_type, code):
    run = saved_run(RunStore())
    keys.secret_set("accounts/vast", "invented-account-key")

    def failed_refresh(*args, **kwargs):
        raise error_type("invented failure\x1b[2J\x07\x9b0m")

    monkeypatch.setattr("ablit.runs.status.refresh", failed_refresh)
    with respx.mock:
        assert main(["status", run["run_id"], "--json"], environ={}) == code
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "invented failure" in captured.err
    assert not any(control in captured.err for control in ("\x1b", "\x07", "\x9b"))


def test_status_and_logs_redact_known_secrets_and_show_key_is_explicit(monkeypatch, capsys):
    store = RunStore()
    r = saved_run(store)
    keys.secret_set("accounts/vast", "invented-account-key")
    keys.secret_set(r["run_id"], "invented-endpoint-key")
    r["untrusted_text"] = "invented-account-key invented-endpoint-key"
    store.save(r)
    monkeypatch.setattr(SSH, "probe", lambda self: {})
    with respx.mock as router:
        router.get(ROOT + "/api/v1/instances/").mock(return_value=page([instance()]))
        assert main(["status", r["run_id"], "--json"], environ={}) == 0
        output = capsys.readouterr().out
        assert "invented-account-key" not in output and "invented-endpoint-key" not in output
        assert "[redacted]" in output
        assert main(["status", r["run_id"], "--json", "--show-key"], environ={}) == 0
        view = json.loads(capsys.readouterr().out)
        assert view["runs"][0]["endpoint_key"] == "invented-endpoint-key"
    class Process:
        stdout = io.StringIO("invented-endpoint-key invented-\x1baccount-key\n")
        def wait(self): return 0
        def poll(self): return 0
    monkeypatch.setattr("ablit.runs.ssh.subprocess.Popen", lambda *a, **kw: Process())
    assert main(["logs", r["run_id"], "--stage", "watchdog"], environ={}) == 0
    output = capsys.readouterr().out
    assert output.count("[redacted]") == 2


@pytest.mark.parametrize("error_type", [FileNotFoundError, PermissionError])
def test_logs_reports_a_local_ssh_start_failure_without_exposing_its_details(monkeypatch, capsys, error_type):
    run = saved_run(RunStore())
    keys.secret_set("accounts/vast", "invented-account-key")

    def fail_start(*args, **kwargs):
        raise error_type("invented-account-key private error details")

    monkeypatch.setattr("ablit.runs.ssh.subprocess.Popen", fail_start)
    assert main(["logs", run["run_id"]], environ={}) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == f"ablit: SSH could not start ({error_type.__name__}).\n"


def test_logs_can_read_bootstrap_output_before_other_stages_start(monkeypatch, tmp_path, capsys):
    import shlex
    import subprocess

    run = saved_run(RunStore())
    keys.secret_set("accounts/vast", "invented-account-key")
    log = tmp_path / "bootstrap.log"
    log.write_text("booting\n", encoding="utf-8")
    popen = subprocess.Popen

    def run_locally(args, **kwargs):
        command = args[-1].replace("/ablit/logs/", shlex.quote(str(tmp_path)) + "/")
        return popen(["/bin/sh", "-c", command], **kwargs)

    monkeypatch.setattr("ablit.runs.ssh.subprocess.Popen", run_locally)
    flags = ["logs", run["run_id"]]
    assert main(flags, environ={}) == 0
    assert "booting\n" in capsys.readouterr().out
    assert main([*flags, "--stage", "engine"], environ={}) == 2
    log.unlink()
    assert main(flags, environ={}) == 2


@pytest.mark.parametrize("with_key", [False, True])
def test_logs_for_a_stopped_run_points_to_the_saved_copy_without_using_ssh(monkeypatch, capsys, with_key):
    store = RunStore()
    run = saved_run(store)
    run["state"] = "billing_stopped_confirmed"
    store.save(run)
    if with_key:
        keys.secret_set("accounts/vast", "invented-account-key")

    def unexpected_ssh(*args, **kwargs):
        pytest.fail("a stopped run must not be contacted over SSH")

    monkeypatch.setattr(SSH, "logs", unexpected_ssh)
    with respx.mock:
        assert main(["logs", run["run_id"]], environ={}) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Billing has stopped for this run." in captured.err
    assert f"ablit logs {run['run_id']} --saved" in captured.err


def test_logs_saved_reads_failure_diagnostics_without_a_marketplace_key(monkeypatch, capsys):
    from ablit.backends import Creds

    store = RunStore()
    run = saved_run(store)
    run["state"] = "billing_stopped_confirmed"
    store.save(run)
    flags = ["logs", run["run_id"], "--saved"]
    status_flags = ["status", run["run_id"], "--json"]
    with respx.mock:
        assert main(flags, environ={}) == 2
        assert "No saved log" in capsys.readouterr().err
        assert main(status_flags, environ={}) == 0
        assert json.loads(capsys.readouterr().out)["runs"][0]["saved_log_available"] is False

        monkeypatch.setattr(SSH, "run", lambda *args, **kwargs: b"bootstrap failed\ninvented-\x1baccount-key\n")
        monitor_mod.save_remote_logs(store, run, Creds("invented-account-key"))
        assert f"ablit logs {run['run_id']} --saved" in capsys.readouterr().out
        assert main(status_flags, environ={}) == 0
        assert json.loads(capsys.readouterr().out)["runs"][0]["saved_log_available"] is True
        assert main(flags, environ={}) == 0
        assert capsys.readouterr().out == "bootstrap failed\n[redacted]\n"
        assert main([*flags, "--stage", "bootstrap"], environ={}) == 2
        assert "--saved cannot be combined" in capsys.readouterr().err


def test_saved_logs_defaults_to_the_last_started_run_when_all_runs_are_stopped(capsys):
    store = RunStore()
    for run_id, started_at in (("z-older", 1000), ("a-newer", 2000)):
        store.save({"run_id": run_id, "state": "billing_stopped_confirmed", "started_at": started_at})
        path = store.saved_log_path(run_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{run_id} failed\n", encoding="utf-8")
    store.save({"run_id": "zz-undated", "state": "billing_stopped_confirmed"})

    with respx.mock:
        assert main(["logs", "--saved"], environ={}) == 0
        assert capsys.readouterr().out == "a-newer failed\n"
        assert main(["logs", "z-older", "--saved"], environ={}) == 0
        assert capsys.readouterr().out == "z-older failed\n"


@pytest.mark.parametrize(("state", "code", "message"), [
    ("creating", 2, "The run is still starting."),
    ("termination_requested", 7, "Cleanup is pending."),
    ("resources_terminated", 7, "Cleanup is pending."),
    ("billing_stopped_confirmed", 2, "Billing has stopped for this run."),
])
def test_tunnel_reports_a_non_running_run_before_asking_for_credentials(capsys, state, code, message):
    store = RunStore()
    run = saved_run(store)
    run["state"] = state
    store.save(run)
    with respx.mock:
        assert main(["tunnel", run["run_id"]], environ={}) == code
    captured = capsys.readouterr()
    assert captured.out == ""
    assert message in captured.err
    if state in ("termination_requested", "resources_terminated"):
        assert f"ablit down {run['run_id']}" in captured.err


def test_tunnel_for_a_running_run_resolves_credentials_and_returns_the_monitor_result(monkeypatch):
    run = saved_run(RunStore())
    with respx.mock:
        assert main(["tunnel", run["run_id"]], environ={}) == 4
    keys.secret_set("accounts/vast", "invented-account-key")
    calls = []

    def monitor(store, record, driver, creds):
        calls.append((record["run_id"], driver.name, creds.api_key))
        return 8

    monkeypatch.setattr("ablit.runs.tunnel.monitor", monitor)
    with respx.mock:
        assert main(["tunnel", run["run_id"]], environ={}) == 8
    assert calls == [(run["run_id"], "vast", "invented-account-key")]


def test_saving_a_failure_log_keeps_the_previous_copy_if_replacement_fails(monkeypatch):
    from ablit.backends import Creds

    store = RunStore()
    run = saved_run(store)
    creds = Creds("invented-account-key")
    monkeypatch.setattr(SSH, "run", lambda *args, **kwargs: b"first log\n")
    monitor_mod.save_remote_logs(store, run, creds)
    path = store.saved_log_path(run["run_id"])
    assert path.stat().st_mode & 0o777 == 0o600

    def fail_replace(source, destination):
        raise OSError("invented rename failure")

    monkeypatch.setattr("ablit.runs.store.os.replace", fail_replace)
    monkeypatch.setattr(SSH, "run", lambda *args, **kwargs: b"replacement log\n")
    monitor_mod.save_remote_logs(store, run, creds)
    assert path.read_text(encoding="utf-8") == "first log\n"
    assert not list(path.parent.glob(".pending-*"))


def test_status_shows_the_cleaned_remote_progress(monkeypatch, capsys):
    store = RunStore()
    r = saved_run(store)
    keys.secret_set("accounts/vast", "invented-account-key")
    remote = {"download": {"bytes_done": 5, "bytes_total": 10, "mbps": 941.5, "files_done": 1, "files_total": 4,
                           "at": 1.0, "extra": "dropped"},
              "join": {"bytes_done": -1, "bytes_total": 10},
              "extract": {"bytes_done": "7", "bytes_total": 10},
              "unknown": {"bytes_done": 1, "bytes_total": 2}}
    monkeypatch.setattr(SSH, "probe", lambda self: {"progress": remote})
    with respx.mock as router:
        router.get(ROOT + "/api/v1/instances/").mock(return_value=page([instance()]))
        assert main(["status", r["run_id"], "--json"], environ={}) == 0
    view = json.loads(capsys.readouterr().out)["runs"][0]
    assert view["progress"] == {"download": {"bytes_done": 5, "bytes_total": 10, "mbps": 941.5,
                                              "files_done": 1, "files_total": 4, "at": 1.0}}


def test_clean_progress_drops_everything_it_does_not_know():
    from ablit.runs.lifecycle import clean_progress
    assert clean_progress(None) == {} and clean_progress([]) == {} and clean_progress({"download": 3}) == {}
    assert clean_progress({"download": {"bytes_done": float("nan"), "bytes_total": 5}}) == {}
    assert clean_progress({"join": {"bytes_done": True, "bytes_total": 5}}) == {}
    assert clean_progress({"join": {"bytes_done": 1, "bytes_total": 5, "mbps": float("inf")}}) == {
        "join": {"bytes_done": 1, "bytes_total": 5}}


@pytest.mark.parametrize("field", ["bytes_done", "bytes_total", "mbps", "files_done", "files_total", "at"])
def test_progress_discards_numbers_that_cannot_be_displayed(field):
    from ablit.runs.lifecycle import clean_progress

    item = {"bytes_done": 5, "bytes_total": 10, field: 10 ** 1000}
    cleaned = clean_progress({"download": item, "join": {"bytes_done": 1, "bytes_total": 2}})
    assert cleaned["join"] == {"bytes_done": 1, "bytes_total": 2}
    if field in ("bytes_done", "bytes_total"):
        assert "download" not in cleaned
    else:
        assert cleaned["download"] == {"bytes_done": 5, "bytes_total": 10}


@pytest.mark.parametrize("large", [10 ** 1000, 1e308], ids=["integer-overflow", "percentage-overflow"])
def test_large_remote_progress_does_not_stop_a_monitored_run(monkeypatch, capsys, large):
    from ablit.backends import Creds

    store = RunStore()
    record = saved_run(store)
    progress = {"download": {"bytes_done": large, "bytes_total": large},
                "join": {"bytes_done": 1, "bytes_total": 2}}
    class Tunnel:
        def poll(self): return None
        def terminate(self): pass
        def wait(self, timeout=None): return 0

    monkeypatch.setattr(SSH, "probe", lambda self: {"progress": progress})
    monkeypatch.setattr(SSH, "open_tunnel", lambda *args: (Tunnel(), 39001))
    monkeypatch.setattr(monitor_mod, "down", lambda *args: pytest.fail("progress must not stop a rental"))
    monkeypatch.setattr(monitor_mod, "save_remote_logs", lambda *args: None)

    def interrupt(seconds): raise KeyboardInterrupt

    monkeypatch.setattr(monitor_mod.time, "sleep", interrupt)
    assert monitor_mod.monitor(store, record, _RunningDriver(), Creds("invented-account-key")) == 0
    saved = store.load(record["run_id"])
    assert saved["spend_estimate_usd"] >= 0 and saved["state"] == "running"
    output = capsys.readouterr().out
    assert "join 50 percent" in output
    if isinstance(large, float):
        assert "download 100 percent" in output
    else:
        assert "download" not in saved["progress"]


def test_the_remote_probe_reads_every_progress_file_and_skips_a_broken_one(tmp_path):
    import subprocess
    from ablit.runs.ssh import PROBE
    (tmp_path / "progress").mkdir()
    (tmp_path / "progress" / "download.json").write_text('{"bytes_done": 5, "bytes_total": 10}')
    (tmp_path / "progress" / "join.json").write_text('{"bytes_done": ')
    (tmp_path / "stages").mkdir()
    (tmp_path / "stages" / "04-speed.done").write_text("")
    script = PROBE.replace("/ablit/", str(tmp_path) + "/")
    result = subprocess.run([sys.executable, "-I", "-c", script], capture_output=True, text=True, check=True)
    probe = json.loads(result.stdout)
    assert probe["progress"] == {"download": {"bytes_done": 5, "bytes_total": 10}}
    assert probe["stages"] == ["04-speed.done"]


@pytest.mark.parametrize("incomplete", ["heartbeat", "ready", "failed", "speed"])
def test_an_incomplete_status_file_does_not_hide_other_probe_data(tmp_path, incomplete):
    import subprocess
    from ablit.runs.ssh import PROBE
    status = {
        "heartbeat": {"at": 1, "spend": 0.25, "action": "continue"},
        "ready": {"ready_at": 1, "port": 8000},
        "failed": {"reason": "check", "detail": "disk full"},
        "speed": {"measured_mbps": 1000},
    }
    for name, data in status.items():
        (tmp_path / f"{name}.json").write_text(json.dumps(data))
    path = tmp_path / f"{incomplete}.json"
    path.write_text('{"incomplete": ')
    script = PROBE.replace("/ablit/", str(tmp_path) + "/")
    result = subprocess.run([sys.executable, "-I", "-c", script], capture_output=True, text=True, check=True)
    probe = json.loads(result.stdout)
    assert incomplete not in probe
    assert {name: probe[name] for name in status if name != incomplete} == {
        name: data for name, data in status.items() if name != incomplete
    }
    path.write_text(json.dumps(status[incomplete]))
    result = subprocess.run([sys.executable, "-I", "-c", script], capture_output=True, text=True, check=True)
    assert json.loads(result.stdout)[incomplete] == status[incomplete]


def test_status_reports_exit_8_from_saved_watchdog_evidence_without_a_marketplace_key(capsys):
    store = RunStore()
    r = saved_run(store)
    r.update(state="billing_stopped_confirmed", stop_reason="watchdog_cap")
    store.save(r)
    with respx.mock:
        assert main(["status", r["run_id"], "--json"], environ={}) == 8
    assert json.loads(capsys.readouterr().out)["runs"][0]["stop_reason"] == "watchdog_cap"


def test_down_can_confirm_a_saved_billing_stop_without_a_marketplace_key(capsys):
    store = RunStore()
    run = saved_run(store)
    with respx.mock:
        assert main(["down", run["run_id"]], environ={}) == 4
        run["state"] = "billing_stopped_confirmed"
        store.save(run)
        assert main(["down", run["run_id"]], environ={}) == 0
    assert "Billing stopped." in capsys.readouterr().out


def test_status_all_keeps_an_earlier_pending_cleanup_exit_code(capsys):
    store = RunStore()
    saved_run(store)
    saved_run(store)
    pending, stopped = store.all()
    stopped.update(resource_ids=["7202"], watchdog_key_id="7402")
    store.save(stopped)
    keys.secret_set("accounts/vast", "invented-account-key")
    with respx.mock as router:
        router.get(ROOT + "/api/v1/instances/").mock(return_value=page([]))
        router.delete(ROOT + "/api/v0/instances/7201/").respond(404)
        router.delete(ROOT + "/api/v0/instances/7202/").respond(404)
        router.delete(ROOT + "/api/v0/auth/apikeys/7401/").respond(403)
        router.delete(ROOT + "/api/v0/auth/apikeys/7402/").respond(200, json="Successfully Deleted API Key")
        assert main(["status", "--all", "--json"], environ={}) == 7
    runs = json.loads(capsys.readouterr().out)["runs"]
    assert runs[0]["run_id"] == pending["run_id"]
    assert runs[0]["pending_cleanup"] == ["Watchdog key deletion is unconfirmed."]
    assert runs[1]["state"] == "billing_stopped_confirmed"


@pytest.mark.parametrize("retry", [False, True])
@pytest.mark.parametrize("prefix", [0, 150])
def test_monitor_redacts_keys_before_shortening_error_messages(monkeypatch, capsys, retry, prefix):
    from ablit.backends import Creds
    from ablit.backends.errors import BackendError

    store = RunStore()
    record = saved_run(store)
    keys.secret_set(record["run_id"], "private-endpoint-key")
    keys.secret_set("watchdog/" + record["run_id"], "private-watchdog-key")
    message = "x" * prefix + "private-runtime-key private-\x1bruntime-key private-endpoint-key private-watchdog-key"
    cleaned = []

    def failed_refresh(*args):
        raise (BackendError if retry else RunError)(message)

    def interrupt(seconds): raise KeyboardInterrupt

    monkeypatch.setattr(monitor_mod, "refresh", failed_refresh)
    monkeypatch.setattr(monitor_mod, "save_remote_logs", lambda *args: None)
    monkeypatch.setattr(monitor_mod, "down", lambda *args: cleaned.append(True) or 0)
    monkeypatch.setattr(monitor_mod.time, "sleep", interrupt)
    code = monitor_mod.monitor(store, record, None, Creds("private-runtime-key"), want_ready=True)
    assert code == (7 if retry else 6) and len(cleaned) == (0 if retry else 1)
    out = capsys.readouterr().out
    assert "private-" not in out and "\x1b" not in out
    assert "[redacted]" in out


@pytest.mark.parametrize("prefix", [0, 150])
def test_cleanup_errors_redact_keys_before_printing_and_saving(monkeypatch, capsys, prefix):
    from ablit.backends import Creds
    from ablit.backends.errors import BackendError
    from ablit.runs.lifecycle import down

    store = RunStore()
    record = saved_run(store)
    keys.secret_set(record["run_id"], "private-endpoint-key")
    keys.secret_set("watchdog/" + record["run_id"], "private-watchdog-key")
    message = "x" * prefix + "private-runtime-key private-\x1bruntime-key private-endpoint-key private-watchdog-key"

    class Driver:
        def reconcile(self, *args): raise BackendError(message)
        def destroy(self, *args): raise BackendError(message)
        def verify_billing_stopped(self, *args): raise BackendError(message)

    assert down(store, record, Driver(), Creds("private-runtime-key")) == 7
    saved = json.dumps(store.load(record["run_id"])["pending_cleanup"])
    out = capsys.readouterr().out
    assert "private-" not in out + saved and "\x1b" not in out
    assert "[redacted]" in out


@pytest.mark.parametrize("stubborn", [False, True])
def test_up_monitor_starts_bootstrap_checks_health_opens_tunnel_and_keeps_billing_on_interrupt(monkeypatch, capsys, stubborn):
    store = RunStore()
    r = saved_run(store)
    keys.secret_set(r["run_id"], "invented-endpoint-key")
    keys.secret_set("watchdog/" + r["run_id"], "invented-scoped-token")
    from ablit.backends import Creds
    class Tunnel:
        killed = False
        def poll(self): return None
        def terminate(self): events.append("tunnel-closed")
        def wait(self, timeout=None):
            assert timeout is not None
            if stubborn and not self.killed:
                import subprocess
                raise subprocess.TimeoutExpired("ssh", timeout)
            return 0
        def kill(self):
            self.killed = True
            events.append("tunnel-killed")
    events = []
    class Remote:
        def __init__(self, *args): pass
        def run(self, command): events.append("tools")
        def upload(self, archive):
            assert b"invented-endpoint-key" not in archive
            events.append("upload")
        def start(self, config, key, watchdog):
            assert config["instance_id"] == 7201
            assert key == "invented-endpoint-key" and watchdog == "invented-scoped-token"
            events.append("start")
        def open_tunnel(self, port):
            events.append("tunnel")
            return Tunnel(), 18001
    def refreshed(store, record, driver, creds):
        record.update(ready=True, marketplace_state="running", stages=["09-ready.done"], spend_estimate_usd=0.01)
        store.save(record)
    monkeypatch.setattr(monitor_mod, "SSH", Remote)
    monkeypatch.setattr(monitor_mod, "refresh", refreshed)
    monkeypatch.setattr(monitor_mod.engines, "health", lambda *args: events.append("health"))
    def interrupt(seconds): raise KeyboardInterrupt
    monkeypatch.setattr(monitor_mod.time, "sleep", interrupt)
    assert monitor_mod.monitor(store, r, None, Creds("invented-account-key"), want_ready=True) == 0
    assert events == ["tools", "upload", "start", "tunnel", "health", "tunnel-closed"] + (["tunnel-killed"] if stubborn else [])
    out = capsys.readouterr().out
    assert "Endpoint: http://127.0.0.1:18001/v1" in out and "Billing continues" in out
    assert store.load(r["run_id"])["state"] == "running"
    assert "local_port" not in store.load(r["run_id"])


@pytest.mark.parametrize("replacement", [False, True])
def test_tunnel_exit_clears_only_its_own_saved_endpoint(monkeypatch, replacement):
    from ablit.backends import Creds

    store = RunStore()
    record = saved_run(store)
    record["state"] = "running"
    store.save(record)

    class Tunnel:
        def poll(self): return None
        def terminate(self): pass
        def wait(self, timeout=None):
            if replacement:
                # A newer monitor may reuse the same port after this tunnel closes.
                latest = store.load(record["run_id"])
                latest.update(local_port=18001, local_tunnel_id="newer-monitor")
                store.save(latest)
            return 0

    class Remote:
        def __init__(self, *args): pass
        def open_tunnel(self, port): return Tunnel(), 18001

    def refreshed(store, record, driver, creds):
        record.update(marketplace_state="running", spend_estimate_usd=0.01)
        store.save(record)

    def interrupt(seconds): raise KeyboardInterrupt

    monkeypatch.setattr(monitor_mod, "SSH", Remote)
    monkeypatch.setattr(monitor_mod, "refresh", refreshed)
    monkeypatch.setattr(monitor_mod.time, "sleep", interrupt)
    assert monitor_mod.monitor(store, record, None, Creds("invented-account-key")) == 0
    latest = store.load(record["run_id"])
    assert latest["state"] == "running"
    if replacement:
        assert latest["local_port"] == 18001 and latest["local_tunnel_id"] == "newer-monitor"
    else:
        assert "local_port" not in latest and "local_tunnel_id" not in latest


def test_up_observes_watchdog_decision_and_absence_before_exit_8(monkeypatch):
    store = RunStore()
    r = saved_run(store)
    from ablit.backends import Creds
    def refreshed(store, record, driver, creds):
        record.update(stop_reason="watchdog_cap", marketplace_state="absent")
        store.save(record)
    monkeypatch.setattr(monitor_mod, "refresh", refreshed)
    with respx.mock as router:
        router.get(ROOT + "/api/v1/instances/").mock(return_value=page([]))
        router.delete(ROOT + "/api/v0/instances/7201/").respond(404)
        router.delete(ROOT + "/api/v0/auth/apikeys/7401/").respond(200, json="Successfully Deleted API Key")
        assert monitor_mod.monitor(store, r, VastBackend(httpx.Client()), Creds("invented-account-key"), want_ready=True) == 8
    assert store.load(r["run_id"])["state"] == "billing_stopped_confirmed"


def test_watchdog_key_failure_cleans_up_the_paid_instance(monkeypatch):
    launch_environment(monkeypatch)
    monkeypatch.setattr("builtins.input", lambda prompt: "rent")
    with respx.mock(assert_all_called=False) as router:
        create, scoped = launch_routes(router)
        scoped.respond(503)
        count = []
        def listing(request):
            count.append(1)
            r = RunStore().all()[0]
            return page([instance(label="ablit-" + r["run_id"])]) if len(count) < 3 else page([])
        router.get(ROOT + "/api/v1/instances/").mock(side_effect=listing)
        destroy = router.delete(ROOT + "/api/v0/instances/7201/").respond(200, json={"success": True})
        router.delete(ROOT + "/api/v0/ssh/7501/").respond(200, json={"success": True})
        assert main(["up", MODEL, "--untested", "--max-spend", "5"], environ={}) == 7
        assert destroy.call_count == 1
    assert "watchdog key response was lost" in " ".join(RunStore().all()[0]["pending_cleanup"])


def test_down_while_locked_returns_cleanup_pending_without_confirmation(monkeypatch):
    store = RunStore()
    r = saved_run(store)
    keys.secret_set("accounts/vast", "invented-account-key")
    monkeypatch.setattr(down_mod, "LOCK_WAIT_SECONDS", 0.5)
    with store.lock(r["run_id"]), respx.mock:
        assert main(["down", r["run_id"]], environ={}) == 7


def test_down_waits_for_an_attached_up_to_release_the_lock(monkeypatch):
    import threading
    store = RunStore()
    r = saved_run(store)
    keys.secret_set("accounts/vast", "invented-account-key")
    taken, release = threading.Event(), threading.Event()
    def hold():
        with store.lock(r["run_id"]):
            taken.set()
            release.wait(5)
    holder = threading.Thread(target=hold)
    holder.start()
    taken.wait(5)
    threading.Timer(0.5, release.set).start()
    with respx.mock as router:
        router.get(ROOT + "/api/v1/instances/").mock(return_value=page([]))
        router.delete(ROOT + "/api/v0/instances/7201/").respond(404)
        router.delete(ROOT + "/api/v0/auth/apikeys/7401/").respond(200, json="Successfully Deleted API Key")
        assert main(["down", r["run_id"]], environ={}) == 0
    holder.join()


def test_reliability_and_speed_drift_keep_the_confirmation(monkeypatch):
    launch_environment(monkeypatch)
    monkeypatch.setattr("builtins.input", lambda prompt: "rent")
    monkeypatch.setattr(up_mod, "monitor", lambda *args, **kwargs: 0)
    with respx.mock(assert_all_called=False) as router:
        create, scoped = launch_routes(router)
        router.post(VAST_URL).mock(side_effect=[
            httpx.Response(200, json={"offers": [vast_row(7101, host_id=7301)]}),
            httpx.Response(200, json={"offers": [vast_row(7101, host_id=7301, reliability=0.98, inet_down=1150.0)]})])
        def listing(request):
            return page([instance(label="ablit-" + RunStore().all()[0]["run_id"])])
        router.get(ROOT + "/api/v1/instances/").mock(side_effect=listing)
        assert main(["up", MODEL, "--untested", "--max-spend", "5"], environ={}) == 0
        assert create.call_count == 1


@pytest.mark.parametrize(("change", "flags"), [
    ({"duration": 4510}, []),  # covers startup and use, but not the model download
    ({"inet_down": 400}, []),
    ({"reliability": 0.96}, ["--min-reliability", "0.97"]),
])
def test_up_cancels_when_the_offer_no_longer_meets_the_search_filters(monkeypatch, capsys, change, flags):
    launch_environment(monkeypatch)
    with respx.mock(assert_all_called=False) as router:
        router.post(VAST_URL).mock(side_effect=[
            httpx.Response(200, json={"offers": [vast_row(7101, host_id=7301)]}),
            httpx.Response(200, json={"offers": [vast_row(7101, host_id=7301, **change)]}),
        ])
        assert main(["up", MODEL, "--untested", "--max-spend", "5", "--yes", *flags], environ={}) == 5
        assert [call.request.url.path for call in router.calls] == ["/api/v0/bundles/"] * 2
    assert "The offer no longer meets the search filters." in capsys.readouterr().out
    assert RunStore().all() == []


def test_up_cancels_when_the_updated_speed_makes_the_cap_too_small(monkeypatch, capsys):
    launch_environment(monkeypatch)
    fast = vast_row(7101, base=4, inet_down_cost=0, internet_down_cost_per_tb=0)
    slow = {**fast, "inet_down": 500}
    with respx.mock(assert_all_called=False) as router:
        router.post(VAST_URL).mock(side_effect=[
            httpx.Response(200, json={"offers": [fast]}),
            httpx.Response(200, json={"offers": [slow]}),
        ])
        assert main(["up", MODEL, "--max-spend", "1.90", "--yes"], environ={}) == 5
        assert [call.request.url.path for call in router.calls] == ["/api/v0/bundles/"] * 2
    assert "The updated download speed raises the minimum spend cap" in capsys.readouterr().out
    assert RunStore().all() == []


def test_a_new_instance_without_a_status_is_not_destroyed(monkeypatch):
    store = RunStore()
    r = saved_run(store)
    from ablit.backends import Creds
    def refreshed(store, record, driver, creds):
        record.update(marketplace_state="starting", spend_estimate_usd=0.0)
        store.save(record)
    monkeypatch.setattr(monitor_mod, "refresh", refreshed)
    monkeypatch.setattr(monitor_mod, "down", lambda *args: pytest.fail("a starting instance must not be destroyed"))
    class NotReady:
        def __init__(self, *args): pass
        def run(self, command): raise monitor_mod.RunError("SSH is not up yet")
    monkeypatch.setattr(monitor_mod, "SSH", NotReady)
    def interrupt(seconds): raise KeyboardInterrupt
    monkeypatch.setattr(monitor_mod.time, "sleep", interrupt)
    assert monitor_mod.monitor(store, r, None, Creds("invented-account-key"), want_ready=True) == 7


def test_a_refused_watchdog_key_cleans_up_without_a_lost_key_warning(monkeypatch):
    launch_environment(monkeypatch)
    monkeypatch.setattr("builtins.input", lambda prompt: "rent")
    with respx.mock(assert_all_called=False) as router:
        create, scoped = launch_routes(router)
        scoped.respond(401, json={"success": False, "msg": "invented refusal"})
        count = []
        def listing(request):
            count.append(1)
            r = RunStore().all()[0]
            return page([instance(label="ablit-" + r["run_id"])]) if len(count) < 3 else page([])
        router.get(ROOT + "/api/v1/instances/").mock(side_effect=listing)
        destroy = router.delete(ROOT + "/api/v0/instances/7201/").respond(200, json={"success": True})
        router.delete(ROOT + "/api/v0/ssh/7501/").respond(200, json={"success": True})
        assert main(["up", MODEL, "--untested", "--max-spend", "5"], environ={}) == 4
        assert destroy.call_count == 1
    record = RunStore().all()[0]
    assert record["state"] == "billing_stopped_confirmed" and record["pending_cleanup"] == []


def test_a_run_vast_stopped_is_destroyed_and_its_host_denied(monkeypatch):
    store = RunStore()
    r = saved_run(store)
    r["host_id"] = "7301"
    store.save(r)
    from ablit.backends import Creds
    def refreshed(store, record, driver, creds):
        record.update(marketplace_state="stopped_by_marketplace", spend_estimate_usd=0.0)
        store.save(record)
    monkeypatch.setattr(monitor_mod, "refresh", refreshed)
    downs = []
    monkeypatch.setattr(monitor_mod, "down", lambda *args: downs.append(1) or 0)
    assert monitor_mod.monitor(store, r, None, Creds("invented-account-key"), want_ready=True) == 6
    assert downs == [1] and "7301" in store.denied_hosts()


def test_a_short_marketplace_outage_does_not_end_the_run(monkeypatch, capsys):
    store = RunStore()
    r = saved_run(store)
    from ablit.backends import Creds
    calls = []
    def refreshed(store, record, driver, creds):
        calls.append(1)
        if len(calls) == 1:
            raise httpx.ConnectError("invented outage")
        raise KeyboardInterrupt
    monkeypatch.setattr(monitor_mod, "refresh", refreshed)
    monkeypatch.setattr(monitor_mod, "down", lambda *args: pytest.fail("a short outage must not destroy the run"))
    monkeypatch.setattr(monitor_mod.time, "sleep", lambda seconds: None)
    assert monitor_mod.monitor(store, r, None, Creds("invented-account-key"), want_ready=True) == 7
    assert len(calls) == 2 and "marketplace check failed (ConnectError" in capsys.readouterr().out


@pytest.mark.parametrize("cleanup_code", [0, 7])
@pytest.mark.parametrize("marketplace_state", [None, "running"])
@pytest.mark.parametrize(("observed_rate", "transfer", "fixed", "cap", "estimate"), [
    (None, 0, 0, 0.52, 0.5),
    (2.0, 0.2, 0.1, 1.2, 1.3),
])
def test_spend_cap_is_checked_during_a_marketplace_outage(
    monkeypatch, cleanup_code, marketplace_state, observed_rate, transfer, fixed, cap, estimate,
):
    from ablit.backends import Creds
    store = RunStore()
    record = saved_run(store)
    record.update(started_at=0.0, cap_usd=cap, spend_estimate_usd=0.0, transfer_estimate_usd=transfer)
    if marketplace_state is not None:
        record["marketplace_state"] = marketplace_state
    record["offer"].update(hourly_all_in=1.0, bandwidth_cost_per_gb=0.0, fixed_fees=fixed)
    if observed_rate is not None:
        record["observed_hourly_rate"] = observed_rate
    store.save(record)
    cleanups = []

    def refreshed(*args):
        raise httpx.ReadTimeout("invented outage")

    def interrupt(seconds):
        raise KeyboardInterrupt

    def down(*args):
        cleanups.append(store.load(record["run_id"]))
        return cleanup_code

    monkeypatch.setattr(monitor_mod, "refresh", refreshed)
    monkeypatch.setattr(monitor_mod.time, "time", lambda: 1800.0)
    monkeypatch.setattr(monitor_mod.time, "sleep", interrupt)
    monkeypatch.setattr(monitor_mod, "down", down)
    assert monitor_mod.monitor(store, record, None, Creds("invented-account-key"), want_ready=True) == (cleanup_code or 6)
    assert len(cleanups) == 1
    assert cleanups[0]["stop_reason"] == "local_cap"
    assert cleanups[0]["spend_estimate_usd"] == pytest.approx(estimate)


@pytest.mark.parametrize(("request_seconds", "poll_seconds", "expected_checks"), [(300, 10, 1), (160, 10, 2), (1, 300, 1)])
def test_marketplace_outage_limit_counts_time_spent_waiting_for_requests(
    monkeypatch, request_seconds, poll_seconds, expected_checks,
):
    from ablit.backends import Creds
    store = RunStore()
    record = saved_run(store)
    clock = [0.0]
    checks, cleanups = [], []

    def refreshed(*args):
        checks.append(1)
        clock[0] += request_seconds
        raise httpx.ReadTimeout("invented outage")

    def sleep(seconds):
        clock[0] += poll_seconds

    monkeypatch.setattr(monitor_mod, "refresh", refreshed)
    monkeypatch.setattr(monitor_mod.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(monitor_mod.time, "sleep", sleep)
    monkeypatch.setattr(monitor_mod, "save_remote_logs", lambda *args: None)
    monkeypatch.setattr(monitor_mod, "down", lambda *args: cleanups.append(1) or 0)
    assert monitor_mod.monitor(store, record, None, Creds("invented-account-key"), want_ready=True) == 6
    assert len(checks) == expected_checks
    assert cleanups == [1]


def test_a_successful_marketplace_check_resets_the_outage_timer(monkeypatch):
    from ablit.backends import Creds
    store = RunStore()
    record = saved_run(store)
    clock = [0.0]
    checks = []

    def refreshed(store, record, driver, creds):
        checks.append(1)
        if len(checks) in (1, 3):
            clock[0] += 200
            raise httpx.ConnectError("invented outage")
        if len(checks) == 4:
            raise KeyboardInterrupt
        record.update(marketplace_state="running", spend_estimate_usd=0.0)
        store.save(record)

    class NotReady:
        def __init__(self, *args): pass
        def run(self, command): raise RunError("SSH is not up yet")

    def sleep(seconds):
        clock[0] += seconds

    monkeypatch.setattr(monitor_mod, "refresh", refreshed)
    monkeypatch.setattr(monitor_mod, "SSH", NotReady)
    monkeypatch.setattr(monitor_mod.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(monitor_mod.time, "sleep", sleep)
    monkeypatch.setattr(monitor_mod, "down", lambda *args: pytest.fail("separate short outages must not end the run"))
    assert monitor_mod.monitor(store, record, None, Creds("invented-account-key"), want_ready=True) == 7
    assert len(checks) == 4


class _RunningDriver:
    def observe(self, alloc, creds):
        from ablit.backends.base import Observation
        return Observation(checked_at=1.0, state="running")


@pytest.mark.parametrize("unavailable", [RunError("SSH did not answer"), {"stages": ["unexpected.done"]}, {"failed": "text"}])
def test_a_successful_probe_clears_a_previous_ssh_warning(monkeypatch, unavailable):
    from ablit.backends import Creds
    from ablit.runs.lifecycle import refresh
    store = RunStore()
    record = saved_run(store)
    healthy = {"heartbeat": {"at": 1.0, "spend": 0.25, "action": "continue"}, "stages": ["04-speed.done"]}
    replies = iter([unavailable, healthy, unavailable])

    def probe(self):
        reply = next(replies)
        if isinstance(reply, Exception):
            raise reply
        return reply

    monkeypatch.setattr(SSH, "probe", probe)
    creds = Creds("invented-account-key")
    refresh(store, record, _RunningDriver(), creds)
    assert store.load(record["run_id"])["ssh_check"] == "unavailable"
    refresh(store, record, _RunningDriver(), creds)
    recovered = store.load(record["run_id"])
    assert "ssh_check" not in recovered
    assert recovered["heartbeat"] == healthy["heartbeat"]
    assert recovered["stages"] == ["04-speed.done"]
    refresh(store, record, _RunningDriver(), creds)
    assert store.load(record["run_id"])["ssh_check"] == "unavailable"


@pytest.mark.parametrize("reply", [[], "text", {"heartbeat": "text"}, {"heartbeat": [1]}, {"failed": "text"}])
def test_a_probe_reply_with_the_wrong_shape_keeps_the_run_loop_going(monkeypatch, reply):
    from ablit.backends import Creds
    from ablit.runs.lifecycle import refresh
    store = RunStore()
    r = saved_run(store)
    monkeypatch.setattr(SSH, "probe", lambda self: reply)
    record = refresh(store, r, _RunningDriver(), Creds("invented-account-key"))
    assert record["ssh_check"] == "unavailable" and "failure" not in record


@pytest.mark.parametrize("stages", [[[]], [{}], ["04-speed.done", {"stage": "05-download.done"}]])
def test_invalid_stage_entries_do_not_interrupt_spending_checks(monkeypatch, stages):
    from ablit.backends import Creds
    from ablit.runs.lifecycle import local_stop_needed, refresh
    store = RunStore()
    record = saved_run(store)
    record.update(cap_usd=0.01, started_at=1.0)
    monkeypatch.setattr(SSH, "probe", lambda self: {"stages": stages})
    refreshed = refresh(store, record, _RunningDriver(), Creds("invented-account-key"))
    assert refreshed["ssh_check"] == "unavailable"
    assert "failure" not in refreshed
    assert local_stop_needed(refreshed, 3601.0)
    assert store.load(record["run_id"])["spend_estimate_usd"] == refreshed["spend_estimate_usd"]


def test_a_remote_failure_detail_loses_its_terminal_control_characters(monkeypatch):
    from ablit.backends import Creds
    from ablit.runs.lifecycle import refresh
    store = RunStore()
    r = saved_run(store)
    detail = "disk full\x1b]52;c;aW52ZW50ZWQ=\x07\x1b[2J"
    monkeypatch.setattr(SSH, "probe", lambda self: {"failed": {"reason": "check", "detail": detail}})
    record = refresh(store, r, _RunningDriver(), Creds("invented-account-key"))
    assert record["failure"]["detail"] == "check: disk full]52;c;aW52ZW50ZWQ=[2J"
