"""Poll a paid run until it is ready, stopped or interrupted."""

from __future__ import annotations

import subprocess
import time

import httpx

from ablit import engines
from ablit.backends.errors import BackendError
from ablit.credentials import CredentialError, secret_get
from ablit.runs import RunBusy, RunError
from ablit.runs.lifecycle import down, local_stop_needed, reason, refresh, save_remote_logs
from ablit.runs.ssh import SSH

POLL_SECONDS = 10
# How long the monitor waits for the run lock before it skips one pass. A `status` or `down` in
# another terminal holds the lock for a short time, and that must not stop a paid run.
MONITOR_LOCK_WAIT_SECONDS = 10
# The longest a machine may take to reach the "running" state, counted from the create. Vast shows
# "loading" while it pulls the image, and one instance stayed there for over 32 minutes. After this
# many seconds ablit destroys the run. Documented in docs/architecture.md.
START_DEADLINE_SECONDS = 20 * 60
# How long the machine may be "running" before ablit has uploaded the bootstrap. Vast bills from
# "running", and sshd normally answers within a minute or two, so five minutes is a generous
# margin. The clock does not depend on an SSH call returning or raising.
SSH_GRACE_SECONDS = 300


def endpoint(record: dict) -> None:
    port, run_id = record["local_port"], record["run_id"]
    print(f"Endpoint: http://127.0.0.1:{port}/v1   (through an SSH tunnel to the rented machine)")
    print(f"API key:  saved in your keyring as ablit/{run_id}. Run `ablit status {run_id} --show-key` to print it.")
    print(f'curl http://127.0.0.1:{port}/v1/chat/completions -H "Authorization: Bearer $ABLIT_KEY" \\\n'
          '  -H "Content-Type: application/json" \\\n'
          f'''  -d '{{"model":"{record['model_id']}","messages":[{{"role":"user","content":"Hello"}}],"max_tokens":8}}' ''')
    print(f"Closing this tunnel doesn't stop billing. Run `ablit down {run_id}` to stop it.", flush=True)


def monitor(store, record, driver, creds, *, want_ready=False):
    """Poll while up, status --watch or tunnel remains attached."""
    run_id = record["run_id"]
    tunnel = None
    uploaded = False
    stage = None
    failures = 0
    first = True
    running_since = None
    shown: dict = {}
    try:
        while True:
            # Each pass lists the account's instances, so poll slowly enough to
            # stay under Vast's rate limit. Waiting here, outside the run lock,
            # lets `ablit down` run from another terminal meanwhile.
            if not first:
                time.sleep(POLL_SECONDS)
            first = False
            try:
                with store.lock(run_id, wait=MONITOR_LOCK_WAIT_SECONDS):
                    pass
            except RunBusy:
                print(f"{run_id}: another ablit command holds the run lock. Skipping this check.", flush=True)
                continue
            with store.lock(run_id, wait=MONITOR_LOCK_WAIT_SECONDS):
                record = store.load(run_id)
                if record["state"] == "billing_stopped_confirmed":
                    if record.get("stop_reason") == "watchdog_cap":
                        return 8
                    if want_ready and tunnel is None:
                        # Another command stopped the run before it had an endpoint.
                        print(f"{run_id}: the run was stopped before the engine was ready.", flush=True)
                        return 6
                    return 0
                try:
                    refresh(store, record, driver, creds)
                    failures = 0
                except (BackendError, httpx.HTTPError) as error:
                    # A short network or marketplace outage should not end a paid
                    # run. The watchdog on the box keeps counting meanwhile. After
                    # five minutes without an answer, stop the run.
                    failures += 1
                    print(f"{run_id}: marketplace check failed ({reason(error)}). Retrying.", flush=True)
                    if failures * POLL_SECONDS < 300:
                        continue
                    raise
                if record.get("stop_reason") == "watchdog_cap" and record["marketplace_state"] == "absent":
                    code = down(store, record, driver, creds)
                    return 8 if code == 0 else code
                # Vast's API notes say "exited", "unknown" and "offline" never reach
                # "running". A new instance with no status yet is "starting", and the
                # bootstrap deadline below still destroys a run that never gets ready.
                # https://docs.vast.ai/api-reference/instances/show-instances
                if record["marketplace_state"] in ("exited", "unknown", "offline", "absent", "stopped_by_marketplace"):
                    if record["marketplace_state"] == "stopped_by_marketplace" and record.get("host_id"):
                        store.deny(record["host_id"], "Vast stopped the instance before it ran")
                    code = down(store, record, driver, creds)
                    return code or 6
                if (record["marketplace_state"] != "running" and not record.get("ready")
                        and time.time() - record["started_at"] > START_DEADLINE_SECONDS):
                    print(f"{run_id}: the marketplace did not start the machine in time "
                          f"(state {record['marketplace_state']!r} after {START_DEADLINE_SECONDS // 60} minutes). "
                          "Destroying the run.", flush=True)
                    if record.get("backend") == "clore" and record.get("host_id") and not record.get("ssh_host"):
                        # Clore listed no public SSH port for this server, so it cannot be reached. Skip it next time.
                        store.deny(record["host_id"], "Clore listed no SSH port before the start deadline")
                    code = down(store, record, driver, creds)
                    return code or 6
                if record.get("failure"):
                    print(f"{run_id}: bootstrap failed: {record['failure'].get('detail', record['failure']['reason'])}", flush=True)
                    save_remote_logs(store, record, creds)
                    if record["failure"]["reason"] == "speed_below_floor" and record.get("host_id"):
                        store.deny(record["host_id"], "speed_below_floor")
                    code = down(store, record, driver, creds)
                    return code or 6
                if local_stop_needed(record, time.time()):
                    record["stop_reason"] = "local_cap"
                    store.save(record)
                    code = down(store, record, driver, creds)
                    return code or 6
                if want_ready and not uploaded and not record.get("ready") and not record.get("stages"):
                    now = time.time()
                    if record["marketplace_state"] == "running":
                        running_since = running_since or now
                        waited = int(now - running_since)
                        if waited > SSH_GRACE_SECONDS:
                            print(f"{run_id}: SSH did not answer in {SSH_GRACE_SECONDS} seconds after the machine was running. "
                                  "Destroying the run.", flush=True)
                            if record.get("host_id"):
                                store.deny(record["host_id"], "SSH did not answer after the machine was running")
                            code = down(store, record, driver, creds)
                            return code or 6
                        print(f"{run_id}: waiting for SSH on the machine ({waited} s)", flush=True)
                    else:
                        print(f"{run_id}: waiting for the marketplace to start the machine "
                              f"(state {record['marketplace_state']!r}, {int(now - record['started_at'])} s)", flush=True)
                if want_ready and not uploaded and record.get("ssh_host"):
                    ssh = SSH(store, record)
                    try:
                        # Wait for the SSH launch environment before copying the scripts.
                        ssh.run("command -v python3 && command -v bash && command -v curl")
                        ssh.upload(engines.bundle(entry_from_record(record), record))
                        key = secret_get(run_id)
                        # A backend with no scoped key (RunPod) has no watchdog key to send.
                        scoped = bool(record.get("watchdog_key_id"))
                        watchdog = secret_get("watchdog/" + run_id) if scoped else ""
                        if not key or (scoped and not watchdog):
                            raise CredentialError("A run secret is missing from the OS keyring. Run down.")
                        backend = record.get("backend", "vast")
                        config = {"run_id": run_id, "model_id": record["model_id"], "backend": backend,
                                  "instance_id": int(record["resource_ids"][0]) if backend == "vast" else record["resource_ids"][0],
                                  "started_at": record["started_at"], "hourly_rate": record["offer"]["hourly_all_in"],
                                  "transfer_price_per_gb": record["offer"]["bandwidth_cost_per_gb"],
                                  "cap_usd": record["cap_usd"], "advertised_mbps": record["offer"]["download_mbps"] or record["min_download_mbps"],
                                  "initial_cost_usd": record["offer"]["bandwidth_cost_per_gb"] * record["quote"]["request"]["image_gb"],
                                  "baseline_rx_bytes": 0}
                        ssh.start(config, key, watchdog)
                        uploaded = True
                    except RunError:
                        # SSH may still be starting. The deadline above ends the wait.
                        pass
                if want_ready and not record.get("ready"):
                    request = record["quote"]["request"]
                    speed = record["offer"]["download_mbps"] or record["min_download_mbps"]
                    allowed = (request["download_gb"] * 8000 / (speed * 3600)
                               + request["startup_hours"] + 10 / 60) * 3600
                    if time.time() - record["started_at"] > allowed:
                        print(f"{run_id}: the bootstrap deadline passed before the engine was ready.", flush=True)
                        save_remote_logs(store, record, creds)
                        code = down(store, record, driver, creds)
                        return code or 6
                current = record.get("stages", [])
                if current != stage:
                    print(f"{run_id}: stages {', '.join(current) or 'waiting for bootstrap'}", flush=True)
                    stage = current
                for line in progress_lines(record, shown):
                    print(f"{run_id}: {line}", flush=True)
                if want_ready and record.get("ready") and tunnel is None:
                    tunnel, port = SSH(store, record).open_tunnel(record["entry"]["profile"]["port"])
                    key = secret_get(run_id)
                    engines.health(f"http://127.0.0.1:{port}", key, record["model_id"])
                    record.update(state="running", local_port=port)
                    store.save(record)
                    endpoint(record)
                    if record.get("report_requested"):
                        from ablit.runs.store import atomic_json
                        atomic_json(store.root / "reports" / (run_id + ".json"),
                                    {"schema": 1, "model_id": record["model_id"], "image": record["image"],
                                     "stages": record.get("stages", []), "ready": True,
                                     "cap_usd": record["cap_usd"], "spend_estimate_usd": record["spend_estimate_usd"]})
                if not want_ready and tunnel is None:
                    tunnel, port = SSH(store, record).open_tunnel(record["entry"]["profile"]["port"])
                    record["local_port"] = port
                    store.save(record)
                    endpoint(record)
                if tunnel is not None and tunnel.poll() is not None:
                    raise RunError("The tunnel closed. Billing continues. Re-run tunnel or down.")
    except KeyboardInterrupt:
        print(f"Billing continues. Run `ablit down {run_id}` to stop it.")
        # Leaving before there is an endpoint is not a success. Billing is not confirmed stopped.
        return 7 if want_ready and tunnel is None else 0
    except (BackendError, RunError, ValueError, httpx.HTTPError, subprocess.SubprocessError) as error:
        print(f"{run_id}: stopping the run because of {reason(error)}", flush=True)
        save_remote_logs(store, store.load(run_id), creds)
        with store.lock(run_id):
            code = down(store, store.load(run_id), driver, creds)
        return code or 6
    finally:
        if tunnel is not None:
            tunnel.terminate()
            tunnel.wait()



def progress_lines(record: dict, shown: dict) -> list[str]:
    """Return one line for each step whose progress passed the next ten percent."""
    lines = []
    for name, item in sorted(record.get("progress", {}).items()):
        total = item.get("bytes_total") or 0
        if total <= 0:
            continue
        step = int(min(item["bytes_done"], total) * 10 / total)
        if step == shown.get(name):
            continue
        shown[name] = step
        text = f"{name} {step * 10} percent, {item['bytes_done'] / 1e9:.1f} of {total / 1e9:.1f} GB"
        if item.get("mbps"):
            text += f", {item['mbps']:.0f} Mbps"
        lines.append(text)
    return lines


def entry_from_record(record):
    from ablit.catalog.models import Entry
    return Entry(**record["entry"])
