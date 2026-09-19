import pytest


@pytest.fixture(autouse=True)
def isolate_runtime_environment(monkeypatch):
    """Keep developer-level routing configuration out of hermetic tests."""
    monkeypatch.delenv("JEV_CONTEXT_CONFIG", raising=False)
    monkeypatch.delenv("JEV_CONTEXT_WORKSPACE_ROOTS", raising=False)
    monkeypatch.delenv("JEV_CONTEXT_METRICS", raising=False)
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)


@pytest.fixture(autouse=True)
def deny_network(monkeypatch):
    """Tests must inject transports; never open an Internet socket."""
    import socket

    def denied(*args, **kwargs):
        raise AssertionError("network disabled in implementation tests")

    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket.socket, "connect_ex", denied)
    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(socket, "getaddrinfo", denied)
