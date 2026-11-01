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
