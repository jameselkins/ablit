"""Use OpenSSH with a private per-run identity and persistent host-key checks."""

from __future__ import annotations

import contextlib
import json
import os
import re
import shlex
import signal
import socket
import subprocess
import time
from pathlib import Path

from ablit.runs import RunError

# The endpoint secret arrives on stdin and exists only in the child's environment.
START = '''import fcntl,json,os,subprocess,sys
payload=json.load(sys.stdin)
os.umask(0o077)
lock=open('/ablit/bootstrap.lock','w')
try:
 fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
except BlockingIOError:
 sys.exit(0)
with open('/ablit/watchdog.key','w') as f: f.write(payload['watchdog_key'])
if not os.path.exists('/ablit/run.json'):
 with open('/ablit/run.json','w') as f: json.dump(payload['config'],f)
env=dict(os.environ,ABLIT_ENDPOINT_KEY=payload['endpoint_key'])
log=open('/ablit/logs/bootstrap.log','ab',buffering=0)
subprocess.Popen(['bash','/ablit/engine/bootstrap.sh'],env=env,stdin=subprocess.DEVNULL,
 stdout=log,stderr=log,start_new_session=True,pass_fds=(lock.fileno(),))
'''

PROBE = '''import glob,json,os
result={}
for name in ('heartbeat','ready','failed','speed'):
 try:
  with open('/ablit/'+name+'.json') as f: result[name]=json.load(f)
 except FileNotFoundError: pass
result['stages']=[os.path.basename(p) for p in sorted(glob.glob('/ablit/stages/*.done'))]
result['progress']={}
for p in sorted(glob.glob('/ablit/progress/*.json')):
 try:
  with open(p) as f: result['progress'][os.path.basename(p)[:-5]]=json.load(f)
 except (OSError,ValueError): pass
print(json.dumps(result))
'''


def _kill_group(proc) -> None:
    """Kill ssh and anything it started, then reap it without waiting on open pipes."""
    with contextlib.suppress(OSError):
        os.killpg(proc.pid, signal.SIGKILL)
    with contextlib.suppress(OSError):
        proc.kill()
    for pipe in (proc.stdin, proc.stdout, proc.stderr):
        with contextlib.suppress(OSError, ValueError):
            if pipe:
                pipe.close()
    with contextlib.suppress(subprocess.SubprocessError):
        proc.wait(timeout=5)


class SSH:
    def __init__(self, store, record):
        self.store, self.record = store, record
        self.directory = store.root / "ssh" / record["run_id"]
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.identity = self.directory / "identity"
        self.known_hosts = self.directory / "known_hosts"

    def identity_public(self) -> str:
        if not self.identity.exists():
            subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", self.record["run_id"],
                            "-f", str(self.identity)], check=True, capture_output=True)
        os.chmod(self.identity, 0o600)
        return self.identity.with_suffix(".pub").read_text().strip()

    def args(self) -> list[str]:
        host, port = self.record.get("ssh_host"), self.record.get("ssh_port")
        if not isinstance(host, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]*", host):
            raise RunError("The marketplace has not supplied a valid SSH host.")
        if type(port) is not int or not 1 <= port <= 65535:
            raise RunError("The marketplace has not supplied a valid SSH port.")
        return ["ssh", "-i", str(self.identity), "-p", str(port),
                "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
                "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=2",
                "-o", "StrictHostKeyChecking=accept-new", "-o", f"UserKnownHostsFile={self.known_hosts}",
                "-o", "GlobalKnownHostsFile=/dev/null", f"root@{host}"]

    def run(self, command: str, data: bytes | None = None, timeout=30) -> bytes:
        """Run one remote command. A timeout or a failure to start ssh is a RunError, like a failed command.

        ssh starts in its own process group. On a timeout the whole group is killed, so a
        helper process that still holds the output pipes cannot keep this call waiting.
        """
        try:
            proc = subprocess.Popen([*self.args(), command], stdin=subprocess.PIPE if data is not None else subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        except OSError as error:
            raise RunError(f"SSH could not start ({error.__class__.__name__}).") from error
        try:
            stdout, _ = proc.communicate(data, timeout=timeout)
        except subprocess.TimeoutExpired as error:
            _kill_group(proc)
            raise RunError(f"SSH did not answer within {timeout} seconds.") from error
        except BaseException:
            _kill_group(proc)
            raise
        if proc.returncode:
            raise RunError("SSH failed. Check the saved host key and the marketplace connection details.")
        return stdout

    def upload(self, archive: bytes) -> None:
        self.run("umask 077; mkdir -p /ablit/logs /ablit/stages /ablit/engine /models; tar -xf - -C /ablit", archive, timeout=60)

    def start(self, config: dict, endpoint_key: str, watchdog_key: str) -> None:
        payload = json.dumps({"config": config, "endpoint_key": endpoint_key, "watchdog_key": watchdog_key}).encode()
        self.run("python3 -c " + shlex.quote(START), payload)

    def probe(self) -> dict:
        return json.loads(self.run("python3 -c " + shlex.quote(PROBE)))

    def open_tunnel(self, remote_port: int):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        args = self.args()
        proc = subprocess.Popen([*args[:-1], "-N", "-o", "ExitOnForwardFailure=yes",
                                 "-L", f"127.0.0.1:{port}:127.0.0.1:{remote_port}", args[-1]],
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                if proc.poll() is not None:
                    raise RunError("The SSH tunnel exited before it opened.")
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                        return proc, port
                except OSError:
                    time.sleep(0.2)
            raise RunError("The SSH tunnel did not open.")
        except BaseException:
            proc.terminate()
            proc.wait()
            raise

    def logs(self, stage: str | None, follow: bool, redactor) -> None:
        files = [stage] if stage else ["bootstrap", "engine", "watchdog"]
        command = "tail " + ("-F " if follow else "-n 200 ") + " ".join(f"/ablit/logs/{s}.log" for s in files)
        proc = subprocess.Popen([*self.args(), command], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                text=True, encoding="utf-8", errors="replace")
        try:
            for line in proc.stdout:
                print(redactor(line), end="", flush=True)
            if proc.wait():
                raise RunError("Remote logs are unavailable.")
        finally:
            if proc.poll() is None:
                proc.terminate()
                proc.wait()
