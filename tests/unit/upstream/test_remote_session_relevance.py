"""Tests for upstream/remote_session_relevance.py: the SecretStore interim copy.

Same rules as the module: stdlib, pytest and bigfix-remote-session-relevance
only, so they move with it when bigfix-remote-session-relevance#32 ships.
"""

from __future__ import annotations

from bigfix_remote_session_relevance import ConnectionConfig

from bigfix_relevance_debugger.upstream.remote_session_relevance import (
    MemorySecretStore,
    SecretStore,
    secret_account,
)


def test_memory_store_round_trip():
    store = MemorySecretStore()

    assert store.get("a") is None
    store.set("a", "pw")
    assert store.get("a") == "pw"
    store.set("a", "new")
    assert store.get("a") == "new"
    store.delete("a")
    assert store.get("a") is None


def test_deleting_a_missing_secret_is_not_an_error():
    MemorySecretStore().delete("never-set")


def test_memory_store_starts_from_a_copy_and_never_shows_secrets():
    seed = {"a": "pw"}
    store = MemorySecretStore(seed)
    store.set("b", "other")

    assert seed == {"a": "pw"}
    assert "pw" not in repr(store) and "other" not in repr(store)


def test_memory_store_is_a_secret_store():
    store: SecretStore = MemorySecretStore()

    assert isinstance(store.name, str) and store.name


def test_secret_account_is_backend_server_and_username_not_the_name():
    a = ConnectionConfig(name="a", backend="rest", server="bf.example.com", username="Admin")
    renamed = ConnectionConfig(name="b", backend="rest", server="bf.example.com", username="admin")

    assert secret_account(a) == secret_account(renamed) == "rest:bf.example.com:52311:admin"


def test_secret_account_tells_servers_and_users_apart():
    base = ConnectionConfig(name="a", backend="rest", server="bf.example.com", username="u")
    accounts = {
        secret_account(base),
        secret_account(
            ConnectionConfig(name="a", backend="rest", server="x.example", username="u")
        ),
        secret_account(
            ConnectionConfig(name="a", backend="rest", server="bf.example.com", username="v")
        ),
        secret_account(
            ConnectionConfig(name="a", backend="webreports", server="bf.example.com", username="u")
        ),
    }

    assert len(accounts) == 4


def test_secret_account_prefixes_a_scheme_that_is_not_https():
    """As in the library's 1.7.0 (commit b35f716): an https password is never
    found, and sent as cleartext Basic auth, for the same server over http."""
    https = ConnectionConfig(name="a", backend="rest", server="https://bf:52311", username="u")
    http = ConnectionConfig(name="a", backend="rest", server="http://bf:52311", username="u")

    assert secret_account(https) == "rest:bf:52311:u"
    assert secret_account(http) == "http+rest:bf:52311:u"


def test_secret_account_uses_the_normalized_server():
    """Host lowercased, the default port filled in and a trailing /api dropped."""
    servers = ["bf.example.com", "https://BF.Example.com", "https://bf.example.com:52311/api"]

    accounts = {
        secret_account(ConnectionConfig(name="a", backend="rest", server=s, username="u"))
        for s in servers
    }

    assert accounts == {"rest:bf.example.com:52311:u"}
