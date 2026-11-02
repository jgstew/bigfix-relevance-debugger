"""Tests for connections.py: where session relevance and Fast Query connections come from."""

from __future__ import annotations

import dataclasses
import json
import os
import stat
import sys

import pytest
from bigfix_remote_session_relevance import (
    Config,
    ConnectionConfig,
    MemorySecretStore,
    secret_account,
)

from bigfix_relevance_debugger import connections
from bigfix_relevance_debugger.connections import (
    APP_ID,
    AppConnections,
    Connection,
    ConnectionsError,
    ConnectionSource,
    LibraryConfig,
    app_data_dir,
)

SAAS = Connection(name="saas", backend="saas", server="login.bigfix.com", username="me@x.com")
ROOT = Connection(
    name="root",
    backend="rest",
    server="bf.example.com",
    username="api",
    computer_ids=(12, 34),
    computer_query='bes computers whose (name of it starts with "web")',
    use_client_context=True,
)


def store(tmp_path, secrets=None):
    return AppConnections(tmp_path / "app" / "connections.json", secrets or MemorySecretStore())


# --- LibraryConfig -------------------------------------------------------------------


def test_library_config_is_the_library_lookup(monkeypatch):
    found = Config(connections=(ConnectionConfig(name="x", backend="rest", server="h"),))
    monkeypatch.setattr(connections, "load_config", lambda: found)

    source: ConnectionSource = LibraryConfig()

    assert source.config() is found


def test_app_connections_is_a_connection_source(tmp_path):
    source: ConnectionSource = store(tmp_path)

    assert source.config() == Config()


# --- Connection ----------------------------------------------------------------------


def test_connection_round_trips_through_a_dict():
    assert Connection.from_dict(ROOT.to_dict()) == ROOT
    assert Connection.from_dict(json.loads(json.dumps(SAAS.to_dict()))) == SAAS


def test_from_dict_fills_defaults_and_ignores_unknown_keys():
    conn = Connection.from_dict(
        {"name": "n", "backend": "rest", "server": "h", "has_secret": True, "future": 1}
    )

    assert conn == Connection(name="n", backend="rest", server="h")


@pytest.mark.parametrize(
    "data",
    [
        {},
        {"name": "", "backend": "rest", "server": "h"},
        {"name": "  ", "backend": "rest", "server": "h"},
        {"name": "n", "backend": "carrier-pigeon", "server": "h"},
        {"name": "n", "backend": "rest", "server": ""},
        {"name": "n", "backend": "rest", "server": "h", "computer_ids": ["12"]},
        {"name": "n", "backend": "rest", "server": "h", "computer_ids": [True]},
        {"name": "n", "backend": "rest", "server": "h", "username": 3},
        {"name": "n", "backend": "rest", "server": "h", "use_client_context": "yes"},
        {"name": "n", "backend": "rest", "server": "h", "password": "pw"},
        [],
    ],
)
def test_from_dict_rejects_what_is_not_a_connection(data):
    with pytest.raises(ConnectionsError):
        Connection.from_dict(data)


def test_a_connection_has_no_password_field():
    """Secrets live in the SecretStore; the dataclass can't carry one by mistake."""
    assert "password" not in Connection.__dataclass_fields__


def test_connection_config_carries_the_password_it_is_given():
    config = ROOT.connection_config("pw")

    assert config == ConnectionConfig(
        name="root", backend="rest", server="bf.example.com", username="api", password="pw"
    )
    assert SAAS.connection_config(None).sid is None
    assert Connection(name="s", backend="saas", server="h", sid="bf1").connection_config(
        None
    ).sid == ("bf1")


def test_a_blank_username_is_none():
    assert Connection(name="n", backend="rest", server="h").connection_config(None).username is None


def test_only_rest_and_saas_with_computers_can_fast_query():
    assert ROOT.can_fastquery
    assert not SAAS.can_fastquery  # no computers
    assert Connection(name="s", backend="saas", server="h", computer_ids=(1,)).can_fastquery
    assert not Connection(
        name="w", backend="webreports", server="h", computer_ids=(1,)
    ).can_fastquery


def test_fastquery_target():
    target = ROOT.fastquery_target()

    assert target.kind == "fastquery"
    assert target.name == target.connection == "root"
    assert target.computer_ids == (12, 34)
    assert target.computer_query == ROOT.computer_query
    assert target.use_client_context is True


# --- AppConnections ------------------------------------------------------------------


def test_no_file_is_no_connections(tmp_path):
    assert store(tmp_path).load() == []


def test_round_trip_with_a_memory_secret_store(tmp_path):
    secrets = MemorySecretStore()
    app = store(tmp_path, secrets)

    app.upsert(SAAS, password="saas-pw")
    app.upsert(ROOT, password="root-pw")

    again = AppConnections(app.path, secrets)
    assert again.load() == [SAAS, ROOT]
    assert again.has_secret(SAAS) and again.has_secret(ROOT)


def test_the_json_never_contains_a_secret(tmp_path):
    app = store(tmp_path)

    app.upsert(SAAS, password="hunter2-very-secret")
    app.upsert(ROOT, password="another-secret")

    text = app.path.read_text(encoding="utf-8")
    assert "hunter2-very-secret" not in text and "another-secret" not in text
    assert "password" not in text


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions")
def test_the_file_is_private_to_the_user(tmp_path):
    app = store(tmp_path)
    app.upsert(SAAS)

    assert stat.S_IMODE(os.stat(app.path).st_mode) == 0o600


def test_config_builds_connection_configs_with_passwords_from_the_store(tmp_path):
    app = store(tmp_path)
    app.upsert(SAAS, password="saas-pw")
    app.upsert(ROOT)  # no password saved

    config = app.config()

    assert [c.name for c in config.connections] == ["saas", "root"]
    assert [c.password for c in config.connections] == ["saas-pw", None]
    assert config.path is None  # nothing for the library to write deployment ids back to


def test_upsert_replaces_by_name_in_place(tmp_path):
    app = store(tmp_path)
    app.upsert(SAAS)
    app.upsert(ROOT)

    changed = Connection(
        name="saas", backend="saas", server="login.bigfix.com", username="me@x.com", sid="bf9"
    )
    app.upsert(changed)

    assert app.load() == [changed, ROOT]


def test_upsert_without_a_password_keeps_the_saved_one(tmp_path):
    secrets = MemorySecretStore()
    app = store(tmp_path, secrets)
    app.upsert(ROOT, password="pw")

    app.upsert(Connection(**{**ROOT.to_dict(), "computer_ids": (99,)}))

    assert app.config().connections[0].password == "pw"


def test_a_password_never_follows_a_connection_to_another_server(tmp_path):
    """Moving a connection must not send its old password to the new server."""
    secrets = MemorySecretStore()
    app = store(tmp_path, secrets)
    app.upsert(ROOT, password="pw")
    old_account = secret_account(ROOT.connection_config(None))

    moved = Connection(**{**ROOT.to_dict(), "server": "evil.example.com"})
    app.upsert(moved)

    assert app.config().connections[0].password is None
    assert secrets.get(old_account) is None  # nothing uses it any more


def test_remove_deletes_the_connection_and_its_unshared_secret(tmp_path):
    secrets = MemorySecretStore()
    app = store(tmp_path, secrets)
    app.upsert(SAAS, password="pw")
    app.upsert(ROOT, password="root-pw")

    app.remove("saas")

    assert app.load() == [ROOT]
    assert secrets.get(secret_account(SAAS.connection_config(None))) is None
    assert app.config().connections[0].password == "root-pw"


def test_a_secret_two_connections_share_survives_removing_one(tmp_path):
    secrets = MemorySecretStore()
    app = store(tmp_path, secrets)
    app.upsert(SAAS, password="pw")
    other = Connection(**{**SAAS.to_dict(), "name": "saas-bf2", "sid": "bf2"})
    app.upsert(other)

    app.remove("saas")

    assert app.config().connections[0].password == "pw"


def test_removing_an_unknown_name_is_an_error(tmp_path):
    with pytest.raises(ConnectionsError, match="no connection named"):
        store(tmp_path).remove("nope")


def test_a_broken_file_is_an_error_not_a_crash(tmp_path):
    app = store(tmp_path)
    app.path.parent.mkdir(parents=True)
    app.path.write_text("{not json", encoding="utf-8")

    with pytest.raises(ConnectionsError, match="connections.json"):
        app.load()


def test_a_file_with_the_wrong_shape_is_an_error(tmp_path):
    app = store(tmp_path)
    app.path.parent.mkdir(parents=True)
    app.path.write_text(json.dumps({"connections": "nope"}), encoding="utf-8")

    with pytest.raises(ConnectionsError):
        app.load()


def test_fastquery_targets_only_for_connections_that_can(tmp_path):
    app = store(tmp_path)
    app.upsert(SAAS)
    app.upsert(ROOT)

    assert [t.connection for t in app.fastquery_targets()] == ["root"]


# --- app data dir --------------------------------------------------------------------


def test_app_data_dir_override(monkeypatch, tmp_path):
    monkeypatch.setenv("BFRD_APP_DATA", str(tmp_path / "here"))

    assert app_data_dir() == tmp_path / "here"
    assert not (tmp_path / "here").exists()  # looking it up creates nothing


@pytest.mark.parametrize("platform", ["darwin", "ios"])
def test_app_data_dir_on_apple_platforms(monkeypatch, tmp_path, platform):
    monkeypatch.delenv("BFRD_APP_DATA", raising=False)
    monkeypatch.setattr(sys, "platform", platform)

    assert app_data_dir() == tmp_path / "Library" / "Application Support" / APP_ID


def test_app_data_dir_elsewhere_is_under_home(monkeypatch, tmp_path):
    monkeypatch.delenv("BFRD_APP_DATA", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.delenv("APPDATA", raising=False)
    monkeypatch.setattr(sys, "platform", "linux")

    assert app_data_dir() == tmp_path / ".local" / "share" / APP_ID


# --- review: scheme, server validation ------------------------------------------------


def test_a_password_never_follows_a_connection_to_plain_http(tmp_path):
    """https -> http keeps host, port and user; the password must still stay behind."""
    app = store(tmp_path)
    app.upsert(Connection(name="a", backend="rest", server="https://bf:52311", username="u"), "pw")

    app.upsert(Connection(name="a", backend="rest", server="http://bf:52311", username="u"))

    assert app.config().connections[0].password is None


def test_a_new_http_connection_does_not_inherit_an_https_password(tmp_path):
    app = store(tmp_path)
    app.upsert(Connection(name="a", backend="rest", server="https://bf:52311", username="u"), "pw")

    app.upsert(Connection(name="b", backend="rest", server="http://bf:52311", username="u"))

    assert [c.password for c in app.config().connections] == ["pw", None]


@pytest.mark.parametrize("server", ["ftp://x", "bf.example.com:abc"])
def test_from_dict_rejects_a_server_the_library_cannot_use(server):
    with pytest.raises(ConnectionsError):
        Connection.from_dict({"name": "n", "backend": "rest", "server": server})


# --- secret keys: the library's secret_account, unchanged from 0.7.0's copy ---------------
# A changed key would lose every saved password, so these pin it.


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


# --- SystemSecrets -------------------------------------------------------------------


class OsStore(MemorySecretStore):
    """Stands in for the Keychain."""

    name = "keychain"


def test_system_secrets_use_the_os_store(monkeypatch):
    """The library's ``default_secret_store()``: the Keychain on macOS and iOS."""
    os_store = OsStore()
    monkeypatch.setattr(connections, "default_secret_store", lambda: os_store)

    secrets = connections.SystemSecrets()
    secrets.set("a", "pw")

    assert secrets.name == "keychain"
    assert os_store.get("a") == "pw"


def test_system_secrets_stay_in_memory_without_an_os_store(monkeypatch):
    monkeypatch.setattr(connections, "default_secret_store", lambda: None)

    secrets = connections.SystemSecrets()
    secrets.set("a", "pw")

    assert secrets.name == "memory"
    assert secrets.get("a") == "pw"


def test_system_secrets_pick_up_an_os_store_that_appears_later(monkeypatch):
    """A keychain locked at launch: new secrets go to it once found; one kept
    in memory meanwhile is still found until deleted."""
    found: list[OsStore] = []
    monkeypatch.setattr(connections, "default_secret_store", lambda: found[0] if found else None)
    secrets = connections.SystemSecrets()
    secrets.set("early", "pw")

    os_store = OsStore()
    found.append(os_store)
    secrets.set("late", "pw2")

    assert secrets.name == "keychain"
    assert os_store.get("late") == "pw2"
    assert os_store.get("early") is None
    assert secrets.get("early") == "pw"
    secrets.delete("early")
    assert secrets.get("early") is None


# --- SystemPasswords -----------------------------------------------------------------

BASIC = ConnectionConfig(name="root", backend="rest", server="bf.example.com", username="api")


class Fixed:
    def __init__(self, *conns: ConnectionConfig) -> None:
        self.conns = conns

    def config(self) -> Config:
        return Config(connections=self.conns)


def passwords(*conns: ConnectionConfig, secrets=None):
    return connections.SystemPasswords(Fixed(*conns), secrets or OsStore())


def test_system_passwords_read_a_missing_password_from_the_os_store():
    """A username with no password of its own: the library reads the store
    (``password_store = "system"``) instead of failing for want of one."""
    (conn,) = passwords(BASIC).config().connections

    assert conn.password_store == "system"
    assert conn.password is None


@pytest.mark.parametrize(
    "conn",
    [
        dataclasses.replace(BASIC, password="inline"),
        dataclasses.replace(BASIC, password_env="BF_PW"),
        dataclasses.replace(BASIC, username=None),
        dataclasses.replace(BASIC, token="t"),
        dataclasses.replace(BASIC, credentials="local_root_server"),
    ],
    ids=["inline", "env", "no-user", "token", "local-root-server"],
)
def test_system_passwords_leave_connections_that_bring_their_own(conn):
    source = passwords(conn)

    assert source.config().connections == (conn,)
    assert not source.can_supply(conn)


def test_a_typed_password_is_used_in_memory_and_never_saved_before_it_works():
    store = OsStore()
    source = passwords(BASIC, secrets=store)

    source.provide("root", "typed", save=True)

    (conn,) = source.config().connections
    assert conn.password == "typed"
    assert store.get(secret_account(BASIC)) is None


def test_a_typed_password_is_saved_once_the_server_accepts_it():
    store = OsStore()
    source = passwords(BASIC, secrets=store)
    source.provide("root", "typed", save=True)

    saved = source.settle("root", accepted=True)

    assert saved == "keychain"
    assert store.get(secret_account(BASIC)) == "typed"
    assert source.settle("root", accepted=True) is None  # once


def test_a_typed_password_not_to_save_stays_in_memory():
    store = OsStore()
    source = passwords(BASIC, secrets=store)
    source.provide("root", "typed", save=False)

    assert source.settle("root", accepted=True) is None
    assert store.get(secret_account(BASIC)) is None
    assert source.config().connections[0].password == "typed"


def test_a_refused_typed_password_is_forgotten():
    store = OsStore()
    source = passwords(BASIC, secrets=store)
    source.provide("root", "wrong", save=True)

    assert source.settle("root", accepted=False) is None

    assert store.get(secret_account(BASIC)) is None
    assert source.config().connections[0].password_store == "system"


def test_a_password_is_only_typed_for_a_connection_that_can_take_one():
    source = passwords(dataclasses.replace(BASIC, password="inline"))

    with pytest.raises(ConnectionsError, match="root"):
        source.provide("root", "typed", save=True)
    with pytest.raises(ConnectionsError, match="nope"):
        source.provide("nope", "typed", save=True)


def test_system_passwords_name_their_store():
    assert passwords(BASIC).store_name == "keychain"
