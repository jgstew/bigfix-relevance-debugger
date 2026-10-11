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

    def refuse():
        raise AssertionError("a test reached a real container engine lookup")

    monkeypatch.setattr(evaluation, "default_image_source", refuse)


@pytest.fixture(autouse=True)
def _no_real_besapi_connection(monkeypatch):
    """Fail loudly instead of connecting to a real BigFix root server.

    besapi reads credentials from the environment and ~/.besapi.conf, so a
    test that reached it could log in to whatever server the developer has
    configured. The process-wide session connector is reset too, so a
    connection one test made can't leak into the next.
    """
    from besapi import besapi, plugin_utilities

    from bigfix_relevance_debugger import evaluation

    def refuse(*args, **kwargs):
        raise AssertionError("a test reached a real besapi connection")

    monkeypatch.setattr(plugin_utilities, "get_besapi_connection", refuse)
    monkeypatch.setattr(besapi.BESConnection, "login", refuse)
    monkeypatch.setattr(evaluation, "_default_connector", None)
