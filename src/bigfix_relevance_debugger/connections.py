"""Where session relevance and Fast Query connections come from.

The same interface on every platform: a :class:`ConnectionSource` hands the
session library a ``Config``.

* :class:`LibraryConfig`, the desktop default: the library's own lookup (its
  TOML file, the ``BES_*`` env vars, ``~/.besapi.conf``), shared with its CLI.
* :class:`AppConnections`, for the iOS app (and any app that owns its
  connections): a JSON file in the app's data dir that **never holds a
  secret**; passwords come from a ``SecretStore`` (the Keychain on Apple
  platforms) when the ``Config`` is built.
* :class:`SystemPasswords`, the desktop's wrapper around a source: a
  password a connection doesn't bring comes from the OS store, or is typed
  in the app and saved there once the server accepts it. Never a file.

Free of NiceGUI, like ``analysis`` and ``evaluation``: the mobile apps import it.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import sys
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Protocol

from bigfix_remote_client_relevance import Target
from bigfix_remote_session_relevance import (
    Config,
    ConfigError,
    ConnectionConfig,
    MemorySecretStore,
    SecretStore,
    default_secret_store,
    load_config,
    secret_account,
)

logger = logging.getLogger(__name__)

APP_ID = "com.github.jgstew.bigfix-relevance-debugger"
"""The app's data folder name (and the iOS bundle id's stem)."""

APP_DATA_ENV = "BFRD_APP_DATA"
"""Overrides :func:`app_data_dir`, e.g. for tests or a portable install."""

BACKENDS = {
    "saas": "BigFix SaaS",
    "rest": "Root server REST API",
    "explorer": "BigFix Explorer",
    "webreports": "Web Reports",
}
"""The session library's backends, as a connection screen names them."""

FASTQUERY_BACKENDS = frozenset({"saas", "rest"})
"""Backends that can send client relevance to endpoints (Fast Query)."""


def app_data_dir() -> Path:
    """Where the app keeps its own files. Looking it up creates nothing.

    ``~/Library/Application Support/<APP_ID>`` on macOS and iOS (where ``HOME``
    is the app's sandbox container), ``%APPDATA%`` on Windows, else the XDG
    data dir. ``$BFRD_APP_DATA`` wins.
    """
    override = os.environ.get(APP_DATA_ENV)
    if override and not override.isspace():
        return Path(override.strip()).expanduser()
    if sys.platform in ("darwin", "ios"):
        return Path.home() / "Library" / "Application Support" / APP_ID
    if sys.platform == "win32" and os.environ.get("APPDATA"):
        return Path(os.environ["APPDATA"]) / APP_ID
    xdg = os.environ.get("XDG_DATA_HOME")
    return (Path(xdg) if xdg else Path.home() / ".local" / "share") / APP_ID


class ConnectionsError(ValueError):
    """A connection or the connections file is unusable; the message is user-facing."""


class ConnectionSource(Protocol):
    """Where a run's BigFix connections come from."""

    def config(self) -> Config: ...


class LibraryConfig:
    """The library's own lookup: its TOML file, ``BES_*`` env vars, ``~/.besapi.conf``.

    The desktop default, shared with the library's CLI. Raises ``ConfigError``.
    """

    def config(self) -> Config:
        return load_config()


def _text(data: dict[str, Any], key: str, *, required: bool = False) -> str:
    value = data.get(key, "")
    if value is None and not required:
        return ""
    if not isinstance(value, str):
        raise ConnectionsError(f"{key} must be text")
    if required and not value.strip():
        raise ConnectionsError(f"a connection needs a {key}")
    return value.strip()


@dataclass(frozen=True)
class Connection:
    """One connection the app owns. Never a secret: passwords are in the ``SecretStore``."""

    name: str
    backend: str
    """One of ``BACKENDS``."""
    server: str
    username: str = ""
    """The SaaS email, or the operator name."""
    sid: str | None = None
    """BigFix SaaS server id; None is the account's only (or default) server."""
    computer_ids: tuple[int, ...] = ()
    """Fast Query targets: BigFix computer ids."""
    computer_query: str | None = None
    """Fast Query targets: session relevance selecting computers, merged with the ids."""
    use_client_context: bool | None = None
    """Fast Query: evaluate in the agent's client context; None leaves it to the server."""

    @classmethod
    def from_dict(cls, data: object) -> Connection:
        """From JSON (the file, or the bridge). Unknown keys are ignored, and
        a ``password`` is refused: it never belongs beside the connection."""
        if not isinstance(data, dict):
            raise ConnectionsError("a connection must be an object")
        if "password" in data:
            raise ConnectionsError("a connection never holds a password; pass it separately")
        backend = _text(data, "backend", required=True)
        if backend not in BACKENDS:
            raise ConnectionsError(
                f"unknown backend {backend!r}; expected one of {', '.join(BACKENDS)}"
            )
        ids = data.get("computer_ids") or ()
        if not isinstance(ids, list | tuple) or not all(
            isinstance(i, int) and not isinstance(i, bool) for i in ids
        ):
            raise ConnectionsError("computer_ids must be a list of whole numbers")
        context = data.get("use_client_context")
        if context is not None and not isinstance(context, bool):
            raise ConnectionsError("use_client_context must be true, false or null")
        conn = cls(
            name=_text(data, "name", required=True),
            backend=backend,
            server=_text(data, "server", required=True),
            username=_text(data, "username"),
            sid=_text(data, "sid") or None,
            computer_ids=tuple(ids),
            computer_query=_text(data, "computer_query") or None,
            use_client_context=context,
        )
        try:
            conn.connection_config(None).address  # noqa: B018 - the library's own check
        except (ConfigError, ValueError) as exc:
            raise ConnectionsError(f"server {conn.server!r}: {exc}") from exc
        return conn

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["computer_ids"] = list(self.computer_ids)
        return data

    def connection_config(self, password: str | None) -> ConnectionConfig:
        """The session library's connection, with ``password`` from the store."""
        return ConnectionConfig(
            name=self.name,
            backend=self.backend,
            server=self.server,
            username=self.username or None,
            password=password,
            sid=self.sid,
        )

    @property
    def account(self) -> str:
        """Its key in the ``SecretStore``: ``secret_account``, which prefixes a
        non-https scheme, so a password saved for https is never sent over
        plain http."""
        return secret_account(self.connection_config(None))

    @property
    def can_fastquery(self) -> bool:
        """A REST or SaaS connection with computers to send client relevance to."""
        return self.backend in FASTQUERY_BACKENDS and bool(self.computer_ids or self.computer_query)

    def fastquery_target(self) -> Target:
        """The client library's Fast Query target for this connection's computers."""
        return Target(
            kind="fastquery",
            name=self.name,
            connection=self.name,
            computer_ids=self.computer_ids,
            computer_query=self.computer_query,
            use_client_context=self.use_client_context,
        )


class AppConnections:
    """Connections the app owns: JSON in the app's data dir, never secrets.

    Passwords go to ``secrets`` (keyed by ``secret_account``: backend, server
    and username, not the name) and come back when :meth:`config` builds the
    session library's ``Config``. Reads the file on every call: it is tiny,
    and nothing else can then go stale.
    """

    def __init__(self, path: Path, secrets: SecretStore) -> None:
        self.path = path
        self.secrets = secrets

    def load(self) -> list[Connection]:
        """The saved connections, in order; none when there is no file yet."""
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return []
        except OSError as exc:
            raise ConnectionsError(f"cannot read {self.path}: {exc}") from exc
        try:
            data = json.loads(text)
            items = data["connections"]
            if not isinstance(items, list):
                raise TypeError("connections is not a list")
            return [Connection.from_dict(item) for item in items]
        except (ValueError, KeyError, TypeError) as exc:
            raise ConnectionsError(f"{self.path} is not a connections file: {exc}") from exc

    def _save(self, items: list[Connection]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps({"connections": [c.to_dict() for c in items]}, indent=2)
        # Written whole and renamed into place, so a crash never leaves half a file.
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".connections.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(text + "\n")
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def _forget_unused(self, account: str, items: list[Connection]) -> None:
        if all(c.account != account for c in items):
            self.secrets.delete(account)

    def upsert(self, conn: Connection, password: str | None = None) -> None:
        """Add ``conn``, or replace the one with its name in place.

        ``password`` goes to the store; None keeps a saved one for the same
        account. A password never follows a connection moved to another
        server or user: that would send it somewhere it wasn't typed for.
        """
        items = self.load()
        old = next((c for c in items if c.name == conn.name), None)
        if old is None:
            items.append(conn)
        else:
            items[items.index(old)] = conn
        if password is not None:
            self.secrets.set(conn.account, password)
        self._save(items)
        if old is not None and old.account != conn.account:
            self._forget_unused(old.account, items)

    def remove(self, name: str) -> None:
        """Delete a connection, and its password unless another connection shares it."""
        items = self.load()
        old = next((c for c in items if c.name == name), None)
        if old is None:
            raise ConnectionsError(f"no connection named {name!r}")
        items.remove(old)
        self._save(items)
        self._forget_unused(old.account, items)

    def has_secret(self, conn: Connection) -> bool:
        return self.secrets.get(conn.account) is not None

    def config(self) -> Config:
        """The session library's ``Config``: one connection each, passwords from the store.

        No ``path``, so the library never writes discovered deployment ids
        into a file of its own for these.
        """
        return Config(
            connections=tuple(c.connection_config(self.secrets.get(c.account)) for c in self.load())
        )

    def fastquery_targets(self) -> list[Target]:
        """A Fast Query target per connection that has computers to query."""
        return [c.fastquery_target() for c in self.load() if c.can_fastquery]


class SystemSecrets:
    """The session library's ``default_secret_store()``, else memory.

    The library answers None for a store it couldn't open *yet* (a keychain
    locked at launch) and tries again on its next call, so this asks again on
    every use until it gets one. New secrets then go to that store; one kept
    in memory meanwhile is still found until it is deleted.
    """

    def __init__(self) -> None:
        self._memory = MemorySecretStore()
        self._store: SecretStore | None = None
        self.name = self._memory.name
        self._system()

    def _system(self) -> SecretStore | None:
        if self._store is None:
            self._store = default_secret_store()
            if self._store is not None:
                self.name = self._store.name
                logger.info("passwords are kept in %s", self.name)
        return self._store

    def get(self, account: str) -> str | None:
        system = self._system()
        found = system.get(account) if system is not None else None
        return found if found is not None else self._memory.get(account)

    def set(self, account: str, secret: str) -> None:
        (self._system() or self._memory).set(account, secret)

    def delete(self, account: str) -> None:
        self._memory.delete(account)
        system = self._system()
        if system is not None:
            system.delete(account)


@dataclass
class _Typed:
    password: str = field(repr=False)
    save: bool
    account: str
    """``secret_account`` when typed: it is never sent to another server or user."""
    used: bool = False
    """A client was built with it, so a row from that connection can judge it."""
    confirmed: bool = False
    """The server accepted it (and it isn't to be saved): kept in memory."""


class SystemPasswords:
    """``source``'s connections, with passwords kept by the app, never in a file.

    A basic-auth connection with a username and no password of its own
    (inline, ``password_env``) reads it from ``secrets`` the library's way,
    as ``password_store = "system"``. One the user typed in the app is used
    from memory, and saved to ``secrets`` only once a query built with it
    has been accepted (:meth:`settle`), only if the user asked, and never
    for BigFix SaaS: a saved SaaS session signs in without the password.
    """

    def __init__(self, source: ConnectionSource, secrets: SecretStore) -> None:
        self.source = source
        self.secrets = secrets
        self._typed: dict[str, _Typed] = {}

    @property
    def store_name(self) -> str:
        return self.secrets.name

    @staticmethod
    def can_supply(conn: ConnectionConfig) -> bool:
        """Whether the app may supply ``conn``'s password: it has none of its own."""
        return (
            conn.auth == "basic"
            and bool(conn.username)
            and conn.password is None
            and not conn.password_env
        )

    @staticmethod
    def can_save(conn: ConnectionConfig) -> bool:
        """Whether a typed password can be saved once it works: not for SaaS,
        where success may come from a saved session instead."""
        return conn.backend != "saas"

    def config(self) -> Config:
        config = self.source.config()
        return dataclasses.replace(
            config, connections=tuple(self._with_password(c) for c in config.connections)
        )

    def _with_password(self, conn: ConnectionConfig) -> ConnectionConfig:
        if not self.can_supply(conn):
            return conn
        typed = self._typed.get(conn.name)
        if typed is not None and typed.account == secret_account(conn):
            typed.used = True
            return dataclasses.replace(conn, password=typed.password)
        return dataclasses.replace(conn, password_store="system")

    def provide(self, name: str, password: str, *, save: bool) -> None:
        """Use ``password`` for connection ``name`` from now on; save it once it works."""
        conn = next((c for c in self.source.config().connections if c.name == name), None)
        if conn is None:
            raise ConnectionsError(f"no connection named {name!r}")
        if not self.can_supply(conn):
            raise ConnectionsError(f"{name} brings its own password or doesn't use one")
        self._typed[name] = _Typed(password, save and self.can_save(conn), secret_account(conn))

    def settle(self, name: str, *, accepted: bool) -> str | None:
        """After a query on ``name``: save a typed password the server ``accepted``
        (returning the store's name), or forget one it refused. A row from a
        client built before it was typed says nothing about it."""
        typed = self._typed.get(name)
        if typed is None or not typed.used or typed.confirmed:
            return None
        if not accepted:
            del self._typed[name]
            return None
        if not typed.save:
            typed.confirmed = True  # kept in memory until the app quits
            return None
        del self._typed[name]
        self.secrets.set(typed.account, typed.password)
        # Saved: from here it comes from the store like any other.
        return self.secrets.name

    def forget_unconfirmed(self) -> None:
        """After a run: forget typed passwords a client used but no row judged
        (another connection answered by failover), so a wrong one isn't sent
        again on every run; the page asks again if needed."""
        for name, typed in list(self._typed.items()):
            if typed.used and not typed.confirmed:
                del self._typed[name]
