import pytest

from kisetsu.clients import rss


@pytest.fixture(autouse=True)
def no_desktop_notifications(monkeypatch):
    """Completing an episode in a test must never pop a real notification or fetch a cover."""
    monkeypatch.setattr("kisetsu.clients.notify._run", lambda *args, **kwargs: None)


@pytest.fixture(autouse=True)
def no_same_host_delay(monkeypatch):
    """Tests fetch from the same fake host over and over and must not wait between requests."""
    monkeypatch.setattr(rss, "_host_delay", rss.HostDelay(0.0))
