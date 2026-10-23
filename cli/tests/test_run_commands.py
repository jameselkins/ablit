"""Command tests never use a marketplace account or a real SSH host."""

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
from ablit.runs import RunStore
from ablit.runs import commands
from ablit.runs.ssh import SSH
from market_fixtures import FakeVast, vast_row, VAST_URL
from test_vast_lifecycle import ROOT, page, instance

MODEL = "llama31-8b-abliterated-q4km"


def tty(monkeypatch):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True, raising=False)


def launch_environment(monkeypatch):
    tty(monkeypatch)
    monkeypatch.setattr(commands.engines, "check_artifacts", lambda entry: None)
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
    monkeypatch.setattr(commands.engines, "engine_lock", lambda e: {"digest": None})
    with respx.mock:
        assert main(["up", MODEL, "--untested", "--max-spend", "5"], environ={}) == 5


XOLOTL = "ds41-1017-xq-v3-uncensored"
UNVERIFIED = "llama33-70b-fp8-dynamic"  # the status check runs before the engine check


def xolotl_launch(monkeypatch, rows, budget="50", extra=()):
    """Run `up` for a Xolotl entry against invented offers and return the exit code and the create calls."""
    launch_environment(monkeypatch)
    monkeypatch.setattr("builtins.input", lambda prompt: "rent")
    monkeypatch.setattr(commands, "monitor", lambda *args, **kwargs: 0)
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
    lock = commands.engines.engine_lock(resolve(load_catalog().entries, XOLOTL))
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
    real = commands.engines.engine_lock
    monkeypatch.setattr(commands.engines, "engine_lock", lambda e: {**real(e), "digest": None})
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
        with pytest.raises(commands.RunError, match="llama.cpp and Xolotl entries on Vast"):
            commands._up(SimpleNamespace(endpoint="tunnel", backends=None),
                         resolve(load_catalog().entries, "ds41-1017-abl-fp8"), None, None, {}, None)


def test_progress_lines_report_each_ten_percent_once():
    shown = {}
    record = {"progress": {"download": {"bytes_done": 55e9, "bytes_total": 100e9, "mbps": 940.4},
                           "join": {"bytes_done": 0, "bytes_total": 0}}}
    first = commands.progress_lines(record, shown)
    assert first == ["download 50 percent, 55.0 of 100.0 GB, 940 Mbps"]
    assert commands.progress_lines(record, shown) == []
    record["progress"]["download"]["bytes_done"] = 100e9
    assert commands.progress_lines(record, shown) == ["download 100 percent, 100.0 of 100.0 GB, 940 Mbps"]


def test_typed_rent_creates_and_saves_only_nonsecret_records(monkeypatch):
    launch_environment(monkeypatch)
    monkeypatch.setattr("builtins.input", lambda prompt: "rent")
    monkeypatch.setattr(commands, "monitor", lambda *args, **kwargs: 0)
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


def test_yes_rents_without_a_terminal_or_a_prompt(monkeypatch, capsys):
    launch_environment(monkeypatch)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False, raising=False)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False, raising=False)
    monkeypatch.setattr("builtins.input", lambda prompt: pytest.fail("--yes must not prompt"))
    monkeypatch.setattr(commands, "monitor", lambda *args, **kwargs: 0)
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
    monkeypatch.setattr(commands, "monitor", lambda *args, **kwargs: 0)
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
    lock = commands.engines.engine_lock(entry)
    from ablit.plan import PlanReport
    req = request_from_entry(entry, lock["image_gb"])
    from ablit.select import rank_offers
    report = PlanReport(entry, req, PlanOptions(), 5, rank_offers([offer], req, PlanOptions()).ranked,
                        [], [], 1, True, "invented-check", lock["digest"])
    data = commands.make_intent(store, entry, offer, report, SimpleNamespace(max_spend=5, min_download=None), lock)
    data.update(state="running", resource_ids=["7201"], ssh_host="ssh.example.invalid", ssh_port=2201,
                watchdog_key_id="7401", watchdog_key_sent=True, create_sent=True)
    store.save(data)
    return data


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
        stdout = io.StringIO("invented-endpoint-key invented-account-key\n")
        def wait(self): return 0
        def poll(self): return 0
    monkeypatch.setattr("ablit.runs.ssh.subprocess.Popen", lambda *a, **kw: Process())
    assert main(["logs", r["run_id"], "--stage", "watchdog"], environ={}) == 0
    output = capsys.readouterr().out
    assert output.count("[redacted]") == 2


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


def test_status_reports_exit_8_from_saved_watchdog_evidence(monkeypatch, capsys):
    store = RunStore()
    r = saved_run(store)
    r.update(state="billing_stopped_confirmed", stop_reason="watchdog_cap")
    store.save(r)
    keys.secret_set("accounts/vast", "invented-account-key")
    with respx.mock:
        assert main(["status", r["run_id"], "--json"], environ={}) == 8
    assert json.loads(capsys.readouterr().out)["runs"][0]["stop_reason"] == "watchdog_cap"


def test_up_monitor_starts_bootstrap_checks_health_opens_tunnel_and_keeps_billing_on_interrupt(monkeypatch, capsys):
    store = RunStore()
    r = saved_run(store)
    keys.secret_set(r["run_id"], "invented-endpoint-key")
    keys.secret_set("watchdog/" + r["run_id"], "invented-scoped-token")
    from ablit.backends import Creds
    class Tunnel:
        def poll(self): return None
        def terminate(self): events.append("tunnel-closed")
        def wait(self): return 0
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
    monkeypatch.setattr(commands, "SSH", Remote)
    monkeypatch.setattr(commands, "refresh", refreshed)
    monkeypatch.setattr(commands.engines, "health", lambda *args: events.append("health"))
    def interrupt(seconds): raise KeyboardInterrupt
    monkeypatch.setattr(commands.time, "sleep", interrupt)
    assert commands.monitor(store, r, None, Creds("invented-account-key"), want_ready=True) == 0
    assert events == ["tools", "upload", "start", "tunnel", "health", "tunnel-closed"]
    out = capsys.readouterr().out
    assert "Endpoint: http://127.0.0.1:18001/v1" in out and "Billing continues" in out
    assert store.load(r["run_id"])["state"] == "running"


def test_up_observes_watchdog_decision_and_absence_before_exit_8(monkeypatch):
    store = RunStore()
    r = saved_run(store)
    from ablit.backends import Creds
    def refreshed(store, record, driver, creds):
        record.update(stop_reason="watchdog_cap", marketplace_state="absent")
        store.save(record)
    monkeypatch.setattr(commands, "refresh", refreshed)
    with respx.mock as router:
        router.get(ROOT + "/api/v1/instances/").mock(return_value=page([]))
        router.delete(ROOT + "/api/v0/instances/7201/").respond(404)
        router.delete(ROOT + "/api/v0/auth/apikeys/7401/").respond(200, json="Successfully Deleted API Key")
        assert commands.monitor(store, r, VastBackend(httpx.Client()), Creds("invented-account-key"), want_ready=True) == 8
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
    monkeypatch.setattr(commands, "LOCK_WAIT_SECONDS", 0.5)
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
    monkeypatch.setattr(commands, "monitor", lambda *args, **kwargs: 0)
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


def test_a_new_instance_without_a_status_is_not_destroyed(monkeypatch):
    store = RunStore()
    r = saved_run(store)
    from ablit.backends import Creds
    def refreshed(store, record, driver, creds):
        record.update(marketplace_state="starting", spend_estimate_usd=0.0)
        store.save(record)
    monkeypatch.setattr(commands, "refresh", refreshed)
    monkeypatch.setattr(commands, "down", lambda *args: pytest.fail("a starting instance must not be destroyed"))
    class NotReady:
        def __init__(self, *args): pass
        def run(self, command): raise commands.RunError("SSH is not up yet")
    monkeypatch.setattr(commands, "SSH", NotReady)
    def interrupt(seconds): raise KeyboardInterrupt
    monkeypatch.setattr(commands.time, "sleep", interrupt)
    assert commands.monitor(store, r, None, Creds("invented-account-key"), want_ready=True) == 7


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
    monkeypatch.setattr(commands, "refresh", refreshed)
    downs = []
    monkeypatch.setattr(commands, "down", lambda *args: downs.append(1) or 0)
    assert commands.monitor(store, r, None, Creds("invented-account-key"), want_ready=True) == 6
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
    monkeypatch.setattr(commands, "refresh", refreshed)
    monkeypatch.setattr(commands, "down", lambda *args: pytest.fail("a short outage must not destroy the run"))
    monkeypatch.setattr(commands.time, "sleep", lambda seconds: None)
    assert commands.monitor(store, r, None, Creds("invented-account-key"), want_ready=True) == 7
    assert len(calls) == 2 and "marketplace check failed (ConnectError" in capsys.readouterr().out
