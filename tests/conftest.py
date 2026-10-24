import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).parent))


@pytest.fixture(autouse=True)
def isolated_logs(tmp_path, monkeypatch):
    """Tests write logs to a temp folder and never hit the daily cap of the real app."""
    from app import config
    monkeypatch.setattr(config, "LOG_DIR", tmp_path / "logs")
    yield


@pytest.fixture
def sample_bytes():
    def _read(name):
        return (ROOT / "data" / "samples" / name).read_bytes()
    return _read
