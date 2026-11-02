"""Saved connections, kept in the app's own data dir. Never secrets.

On iOS ``HOME`` is the app's sandbox container, so ``~/Library/Application
Support`` is the right place on iOS and macOS alike. The CLI's
``~/.bigfix/.remote_session_connections.toml`` is deliberately not used:
a sandboxed app can't share it, and that file may hold passwords.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

APP_ID = "com.github.jgstew.bigfix-relevance-debugger"

BACKENDS = {
    "saas": "BigFix SaaS",
    "rest": "Root server REST API",
    "explorer": "BigFix Explorer",
    "webreports": "Web Reports",
}
FASTQUERY_BACKENDS = frozenset({"saas", "rest"})
"""Backends that can send Fast Query (client relevance to endpoints)."""


def app_data_dir() -> Path:
    override = os.environ.get("BFRD_APP_DATA")
    if override:
        path = Path(override)
    elif sys.platform in ("darwin", "ios"):
        path = Path.home() / "Library" / "Application Support" / APP_ID
    else:  # Android/Windows/Linux: later
        import platformdirs

        path = Path(platformdirs.user_data_dir(APP_ID))
    path.mkdir(parents=True, exist_ok=True)
    return path


@dataclass
class Connection:
    name: str
    backend: str = "saas"
    server: str = "login.bigfix.com"
    username: str = ""
    sid: str | None = None
    """BigFix SaaS server id; ``None`` is the account's default server."""
    computers: str = ""
    """Fast Query targets: comma-separated computer ids, or session relevance
    that selects computers (``bes computers whose (...)``)."""
    use_client_context: bool | None = None
    extra: dict[str, str] = field(default_factory=dict)

    @property
    def secret_account(self) -> str:
        return f"{self.name}:{self.username}"

    @property
    def can_fastquery(self) -> bool:
        return self.backend in FASTQUERY_BACKENDS


class ConnectionStore:
    """A JSON list of :class:`Connection` at ``<app data>/connections.json``."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or app_data_dir() / "connections.json"

    def load(self) -> list[Connection]:
        if not self.path.is_file():
            return []
        known = {f.name for f in fields(Connection)}
        data = json.loads(self.path.read_text(encoding="utf-8"))
        return [Connection(**{k: v for k, v in item.items() if k in known}) for item in data]

    def save(self, connections: list[Connection]) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps([asdict(c) for c in connections], indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def upsert(self, connection: Connection) -> list[Connection]:
        items = [c for c in self.load() if c.name != connection.name]
        items.append(connection)
        self.save(items)
        return items

    def remove(self, name: str) -> list[Connection]:
        items = [c for c in self.load() if c.name != name]
        self.save(items)
        return items
