"""A credential-free self-check of the headless core, for the iOS app's About screen.

Each step says PASS, FAIL or SKIP with a short detail; none needs a BigFix
server or a password. Only the TLS step uses the network (a bare handshake
and an anonymous ``HEAD`` to BigFix SaaS's login host), and ``network=False``
skips it, as the unit tests do.
"""

from __future__ import annotations

import asyncio
import ssl
import tempfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bigfix_relevance_analyzer import highlight
from bigfix_remote_session_relevance import AsyncSessionRelevanceClient, Config, TrustStore

from bigfix_relevance_debugger.analysis import summarize
from bigfix_relevance_debugger.evaluation import EvalOptions, SessionClients, stream_session
from bigfix_relevance_debugger.upstream.remote_session_relevance import SecretStore

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"
TLS_HOST = "login.bigfix.com"
SECRET_ACCOUNT = "bigfix-relevance-debugger:selfcheck"
TLS_TIMEOUT_S = 10.0


@dataclass(frozen=True)
class Check:
    name: str
    status: str
    """``PASS``, ``FAIL`` or ``SKIP``."""
    detail: str

    @property
    def line(self) -> str:
        return f"{self.status} {self.name}: {self.detail}"


async def _analysis() -> str:
    view = summarize("name of operating system")
    if view.parse_error is not None:
        raise AssertionError(view.parse_error.message)
    spans = highlight(view.text)
    if not spans:
        raise AssertionError("highlight() gave no spans")
    return f"{view.type_summary}, runs as {view.dialect}, {len(spans)} highlight spans"


def _data_dir(path: Path) -> Callable[[], Awaitable[str]]:
    async def check() -> str:
        path.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=path, prefix=".selfcheck.") as handle:
            handle.write(b"ok")
            handle.flush()
            if Path(handle.name).read_bytes() != b"ok":
                raise AssertionError("read back something else")
        return f"{path} is writable"

    return check


def _secrets(store: SecretStore) -> Callable[[], Awaitable[str]]:
    async def check() -> str:
        store.set(SECRET_ACCOUNT, "selfcheck")
        try:
            if store.get(SECRET_ACCOUNT) != "selfcheck":
                raise AssertionError("read back something else")
        finally:
            store.delete(SECRET_ACCOUNT)
        if store.get(SECRET_ACCOUNT) is not None:
            raise AssertionError("delete left the secret behind")
        return f"{store.name} round trip"

    return check


async def _tls() -> str:
    import certifi
    import httpx

    context = ssl.create_default_context(cafile=certifi.where())
    _, writer = await asyncio.wait_for(
        asyncio.open_connection(TLS_HOST, 443, ssl=context, server_hostname=TLS_HOST),
        TLS_TIMEOUT_S,
    )
    version = writer.get_extra_info("ssl_object").version()
    writer.close()
    await writer.wait_closed()
    # Anonymous: no sign-in, no credentials, just "is HTTPS through certifi usable".
    async with httpx.AsyncClient(verify=context, timeout=TLS_TIMEOUT_S) as client:
        response = await client.head(f"https://{TLS_HOST}/")
    return f"{TLS_HOST}: {version}, HEAD {response.status_code}"


async def _config_row() -> str:
    def nothing_configured(opts: EvalOptions) -> AsyncSessionRelevanceClient:
        return AsyncSessionRelevanceClient(
            Config(), prompt=None, trust=TrustStore(trust_on_first_use=False)
        )

    clients = SessionClients(nothing_configured)
    try:
        rows = [ev async for ev in stream_session("now", EvalOptions(), clients=clients)]
    finally:
        await clients.aclose()
    if [ev.error_kind for ev in rows] != ["config"]:
        raise AssertionError(f"expected one config row, got {rows}")
    return "no connections gives the config row"


async def _fastquery() -> str:
    from bigfix_remote_client_relevance import SessionContext
    from bigfix_remote_client_relevance.transports.fastquery import TransportFastQuery

    return f"{SessionContext.__name__} and {TransportFastQuery.__name__} import"


async def run(*, data_dir: Path, secrets: SecretStore, network: bool = True) -> list[Check]:
    """Every step, in order; a failing step is a FAIL line, never an exception."""
    steps: list[tuple[str, Callable[[], Awaitable[str]] | None]] = [
        ("analysis", _analysis),
        ("data dir", _data_dir(data_dir)),
        ("secrets", _secrets(secrets)),
        ("tls", _tls if network else None),
        ("evaluate", _config_row),
        ("fast query", _fastquery),
    ]
    checks = []
    for name, step in steps:
        if step is None:
            checks.append(Check(name, SKIP, "network checks are off"))
            continue
        try:
            checks.append(Check(name, PASS, await step()))
        except Exception as exc:  # noqa: BLE001 - every failure is a line
            checks.append(Check(name, FAIL, f"{type(exc).__name__}: {exc}"))
    return checks


def report(checks: list[Check]) -> dict[str, Any]:
    """The bridge's ``selfcheck`` result: one line per check, then ``SELFCHECK n/m``."""
    passed = sum(c.status == PASS for c in checks)
    skipped = sum(c.status == SKIP for c in checks)
    total = len(checks)
    lines = [c.line for c in checks] + [f"SELFCHECK {passed}/{total - skipped}"]
    return {"lines": lines, "passed": passed, "skipped": skipped, "total": total}
