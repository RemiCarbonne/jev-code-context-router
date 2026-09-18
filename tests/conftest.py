import pytest


@pytest.fixture(autouse=True)
def isolate_runtime_environment(monkeypatch):
    """Keep developer-level routing configuration out of hermetic tests."""
    monkeypatch.delenv("JEV_CONTEXT_CONFIG", raising=False)
    monkeypatch.delenv("JEV_CONTEXT_WORKSPACE_ROOTS", raising=False)
    monkeypatch.delenv("JEV_CONTEXT_METRICS", raising=False)
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
