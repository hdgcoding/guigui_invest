"""Shared test fixtures for guigui_invest (AKShare edition)."""

import os
import sys

import pytest

# Add scripts/ to path so we can import from there
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "fixtures")


@pytest.fixture
def fixtures_dir():
    """Path to the test fixtures directory."""
    return FIXTURES_DIR


@pytest.fixture
def sample_stock_code():
    """Standard test stock code (Yili 伊利股份)."""
    return "600887.SH"


@pytest.fixture
def tmp_output_dir(tmp_path):
    """Temporary output directory for test file generation."""
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    return output_dir


@pytest.fixture
def no_rate_limit(monkeypatch):
    """Disable AKShare rate-limit sleeps in tests."""
    monkeypatch.setenv("AKSHARE_RATE_DELAY", "0")


# Mark integration tests that require live network
integration = pytest.mark.skipif(
    os.environ.get("GUILGUI_INTEGRATION", "") != "1",
    reason="integration tests opt-in only (set GUILGUI_INTEGRATION=1)",
)


def pytest_configure(config):
    """Register custom markers."""
    config.addinivalue_line("markers", "integration: mark test as requiring live network access")


@pytest.fixture(autouse=True)
def _isolate_env_file(monkeypatch, tmp_path):
    """Prevent _load_env_file from finding the real .env during tests."""
    import config as config_mod

    monkeypatch.setattr(config_mod, "__file__", str(tmp_path / "scripts" / "config.py"))
