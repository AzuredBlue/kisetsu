import pytest


@pytest.fixture(autouse=True)
def no_desktop_notifications(monkeypatch):
    """Completing an episode in a test must never pop a real notification or fetch a cover."""
    monkeypatch.setattr("kisetsu.core.notify._run", lambda *args, **kwargs: None)
