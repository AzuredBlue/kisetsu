import pytest

from kisetsu.clients import rss


@pytest.fixture(autouse=True)
def no_desktop_notifications(monkeypatch):
    """Completing an episode in a test must never pop a real notification or fetch a cover."""
    monkeypatch.setattr("kisetsu.clients.notify._run", lambda *args, **kwargs: None)


@pytest.fixture(autouse=True)
def no_rate_limit_wait(monkeypatch):
    """A fake host's 429 must never make a test sleep, nor carry over to the next test."""

    async def sleep(seconds):
        return None

    monkeypatch.setattr(rss, "_host_gate", rss.HostGate(sleep=sleep))
