import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))


import pytest


@pytest.fixture(autouse=True)
def _no_arxiv_wait(monkeypatch):
    """Tests use mocked APIs, so skip the real 3-second arXiv spacing."""
    import rra.http
    monkeypatch.setattr(rra.http, "MIN_INTERVAL", {})
    monkeypatch.setattr(rra.http, "_last_request", {})


@pytest.fixture(autouse=True)
def _no_dns(monkeypatch):
    """Link checks resolve hostnames to block private addresses; tests use no real DNS."""
    import rra.http
    monkeypatch.setattr(rra.http, "_resolve", lambda host: [])


@pytest.fixture(autouse=True)
def _no_dotenv(monkeypatch):
    """Tests never read your real .env (and so never see your API key)."""
    import rra.cli
    monkeypatch.setattr(rra.cli, "load_dotenv", lambda *a, **k: [])
