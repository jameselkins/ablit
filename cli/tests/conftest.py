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
