"""Command tests never use a marketplace account or a real SSH host."""

import io
import json
import sys
from dataclasses import asdict, replace
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
from test_vast_lifecycle import ROOT, RUN, page, instance

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
    with store.lock(r["run_id"]), respx.mock:
        assert main(["down", r["run_id"]], environ={}) == 7


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
        record.update(marketplace_state="unknown", spend_estimate_usd=0.0)
        store.save(record)
    monkeypatch.setattr(commands, "refresh", refreshed)
    monkeypatch.setattr(commands, "down", lambda *args: pytest.fail("a starting instance must not be destroyed"))
    class NotReady:
        def __init__(self, *args): pass
        def run(self, command): raise commands.RunError("SSH is not up yet")
    monkeypatch.setattr(commands, "SSH", NotReady)
    def interrupt(seconds): raise KeyboardInterrupt
    monkeypatch.setattr(commands.time, "sleep", interrupt)
    assert commands.monitor(store, r, None, Creds("invented-account-key"), want_ready=True) == 0
