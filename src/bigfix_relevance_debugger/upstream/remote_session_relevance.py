"""For bigfix-remote-session-relevance#32: a ``SecretStore`` for connection passwords.

Structurally the same as the library's planned ``secrets.py`` (``SecretStore``,
``MemorySecretStore``, ``secret_account``), so its store, the Apple Keychain
one included, drops in for ours once it ships. No Keychain here: that is the
library's to ship.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from bigfix_remote_session_relevance import ConnectionConfig


class SecretStore(Protocol):
    """Where connection secrets live. Keys are opaque account strings."""

    name: str  # for people: "macOS Keychain", "memory"

    def get(self, account: str) -> str | None: ...
    def set(self, account: str, secret: str) -> None: ...
    def delete(self, account: str) -> None:
        """Remove the secret; a missing one is not an error."""
        ...


class MemorySecretStore:
    """A dict: for tests, and for apps that hold secrets themselves."""

    name = "memory"

    def __init__(self, secrets: dict[str, str] | None = None) -> None:
        self._secrets = dict(secrets or {})

    def __repr__(self) -> str:
        return f"MemorySecretStore(<{len(self._secrets)} secrets>)"

    def get(self, account: str) -> str | None:
        return self._secrets.get(account)

    def set(self, account: str, secret: str) -> None:
        self._secrets[account] = secret

    def delete(self, account: str) -> None:
        self._secrets.pop(account, None)


def secret_account(conn: ConnectionConfig) -> str:
    """Stable per-connection key: backend, server and username.

    Not the name, so renaming a connection keeps its secret, and the same
    file lines up with each machine's own store entry. A non-https scheme is
    prefixed (``http+rest:...``) so a password saved for https is never found,
    and sent as cleartext Basic auth, for the same server over http.
    """
    address = conn.address
    prefix = "" if address.scheme == "https" else f"{address.scheme}+"
    return f"{prefix}{conn.backend}:{address.host_port}:{(conn.username or '').lower()}"
