import sys
from pathlib import Path

import pytest

CLI_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = CLI_DIR.parent
TOOLS_DIR = REPO_ROOT / "tools"

for path in (str(TOOLS_DIR),):
    if path not in sys.path:
        sys.path.insert(0, path)


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return REPO_ROOT


@pytest.fixture(scope="session")
def cli_dir() -> Path:
    return CLI_DIR


@pytest.fixture(scope="session")
def entry_files(repo_root: Path) -> list[Path]:
    files = sorted((repo_root / "catalog" / "models").glob("*.yaml"))
    assert files, "no catalog entries found"
    return files


@pytest.fixture(autouse=True)
def isolated_run_state(tmp_path, monkeypatch):
    """Never let tests read an account key or the user's run records."""
    import keyring
    from keyring.backend import KeyringBackend
    from keyring.errors import PasswordDeleteError
    from ablit import credentials

    class MemoryKeyring(KeyringBackend):
        priority = 1
        def __init__(self):
            self.values = {}
        def get_password(self, service, username):
            return self.values.get((service, username))
        def set_password(self, service, username, password):
            self.values[service, username] = password
        def delete_password(self, service, username):
            if (service, username) not in self.values:
                raise PasswordDeleteError("missing")
            del self.values[service, username]

    backend = MemoryKeyring()
    monkeypatch.setattr(keyring, "get_keyring", lambda: backend)
    monkeypatch.setattr(keyring.core, "get_keyring", lambda: backend)
    monkeypatch.setattr("ablit.runs.store.state_dir", lambda: tmp_path / "state")
    monkeypatch.delenv("VAST_API_KEY", raising=False)
    monkeypatch.delenv("RUNPOD_API_KEY", raising=False)
    monkeypatch.delenv("CLORE_API_KEY", raising=False)
    monkeypatch.delenv("LIUM_API_KEY", raising=False)
    monkeypatch.delenv("ABLIT_FAULT", raising=False)
    from ablit.backends.vast import VastBackend
    monkeypatch.setattr(VastBackend, "inventory_interval", 0)
    from ablit.backends.clore import CloreBackend
    monkeypatch.setattr(CloreBackend, "request_interval", 0)
    from ablit.backends.runpod import RunPodBackend
    from ablit.backends.lium import LiumBackend
    for backend_class in (VastBackend, RunPodBackend, CloreBackend, LiumBackend):
        # Retry backoff waits up to 30 seconds, so tests never wait for it.
        monkeypatch.setattr(backend_class, "_sleep", staticmethod(lambda seconds: None))
    credentials.SESSION.clear()
    yield backend
    credentials.SESSION.clear()
