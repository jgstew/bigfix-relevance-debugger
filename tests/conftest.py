"""Shared test scaffolding.

The unit suite runs offline: no Docker, qna, SSH or network.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolated_compat_memory(tmp_path, monkeypatch):
    """Keep bigfix-remote-client-relevance's fallback memory out of the real state dir.

    Evaluation tests run the real orchestrator against fake transports, and
    it persists which qna builds are too new for which target.
    """
    from bigfix_remote_client_relevance.bootstrap import compat_memory

    monkeypatch.setattr(compat_memory, "default_path", lambda: tmp_path / "compat_memory.json")


@pytest.fixture(autouse=True)
def _no_real_auto_discovery(monkeypatch):
    """Fail loudly instead of running the library's real first-run discovery.

    It evaluates candidates, may pull images and writes
    ~/.bigfix/remote_clients.toml -- never something a unit test may do.
    """
    from bigfix_relevance_debugger.upstream import remote_client_relevance

    def refuse(path):
        raise AssertionError(f"a test reached real auto-discovery (would write {path})")

    monkeypatch.setattr(remote_client_relevance, "run_auto_discovery", refuse)


@pytest.fixture(autouse=True)
def _no_real_container_engine(monkeypatch):
    """Fail loudly instead of asking a real docker/podman, and through it a registry.

    The page looks up the ``auto`` arch in the background whenever Container
    is ticked, so a page test that forgot to fake the lookup would otherwise
    reach whatever engine the developer has running.
    """
    from bigfix_relevance_debugger import evaluation

    reached: list[str] = []

    def refuse():
        reached.append("default_image_source")
        raise AssertionError("a test reached a real container engine lookup")

    monkeypatch.setattr(evaluation, "default_image_source", refuse)
    yield
    # Also at teardown: the page's background lookup catches errors (it must
    # not take the page down), which would swallow the raise above.
    assert not reached, "a test reached a real container engine lookup"


@pytest.fixture(autouse=True)
def _no_real_session_relevance_client(tmp_path, monkeypatch):
    """Fail loudly instead of connecting to a real BigFix server, and keep its files out.

    bigfix-remote-session-relevance reads connections from its TOML file,
    the ``BES_*`` env vars and ``~/.besapi.conf``, uses a root server's own
    credentials, writes discovered deployment ids back to its file and pins
    certificates under ``~/.bigfix``. So HOME and every lookup point at
    ``tmp_path``, the default client factory refuses, and the process-wide
    clients are reset so a client one test built can't leak into the next.
    """
    from bigfix_relevance_debugger import evaluation

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("BFRSR_CONFIG", str(tmp_path / "no-session-connections.toml"))
    monkeypatch.setenv("BFRSR_LOCAL_ROOT_SERVER", "0")
    for name in ("BES_ROOT_SERVER", "BES_USER_NAME", "BES_PASSWORD"):
        monkeypatch.delenv(name, raising=False)

    def refuse(*args, **kwargs):
        raise AssertionError("a test reached a real session relevance client")

    monkeypatch.setattr(evaluation, "default_session_client", refuse)
    monkeypatch.setattr(evaluation, "_session_clients", None)
