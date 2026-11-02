"""A self-check every PoC runs the same way (``BFRD_SMOKE=1``), with no taps.

It proves the platform plumbing the real features depend on without any
BigFix credentials: no login is attempted, so nothing can count toward a
SaaS lockout. The one network touch is a TLS handshake plus an anonymous
HEAD to the public BigFix SaaS login page.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable

from bfrd_mobile_core import analysis, versions
from bfrd_mobile_core.connections import ConnectionStore, app_data_dir
from bfrd_mobile_core.evaluate import Evaluator
from bfrd_mobile_core.secrets import MemorySecretStore, default_secret_store

PUBLIC_TLS_HOST = "login.bigfix.com"


async def run(report: Callable[[str], None] | None = None) -> list[str]:
    lines: list[str] = []

    def say(line: str) -> None:
        lines.append(line)
        if report is not None:
            report(line)

    async def step(name: str, fn) -> None:
        started = time.perf_counter()
        try:
            detail = await fn()
            say(f"PASS {name} ({(time.perf_counter() - started) * 1000:.0f} ms): {detail}")
        except Exception as exc:  # noqa: BLE001 - a smoke test reports everything
            say(f"FAIL {name}: {type(exc).__name__}: {exc}")

    async def v() -> str:
        return ", ".join(f"{k} {val}" for k, val in versions().items())

    async def analyze() -> str:
        a = analysis.analyze('names of bes computers whose (name of it contains "web")')
        b = analysis.analyze("name of operating sytem")
        assert a.dialects == ("session",), a.dialects
        assert b.findings and b.findings[0].code == "unknown-inspector"
        return f"{a.summary} | {len(a.spans)} spans | typo caught"

    async def data_dir() -> str:
        store = ConnectionStore()
        store.load()
        probe = app_data_dir() / "smoke.txt"
        probe.write_text("ok")
        probe.unlink()
        return str(app_data_dir())

    async def keychain() -> str:
        store = default_secret_store()
        if store.name != "Keychain":
            raise RuntimeError("Keychain unavailable; fell back to memory")
        account = f"smoke-{uuid.uuid4()}"
        store.set(account, "s3cret")
        try:
            assert store.get(account) == "s3cret"
        finally:
            store.delete(account)
        assert store.get(account) is None
        return "set/get/delete round trip"

    async def tls() -> str:
        import httpx
        from bigfix_remote_session_relevance.trust import fetch_server_cert

        info = await fetch_server_cert(PUBLIC_TLS_HOST, 443)
        async with httpx.AsyncClient(timeout=10) as client:  # certifi CA bundle
            response = await client.head(f"https://{PUBLIC_TLS_HOST}/")
        return f"HTTP {response.status_code}; TOFU view sha256 {info.fingerprint[:23]}…"

    async def evaluate_unconfigured() -> str:
        ev = Evaluator(MemorySecretStore(), known_certs_path=str(app_data_dir() / "smoke.toml"))
        rows = [r async for r in ev.run("now", ("session", "client"), [])]
        assert [r.error_kind for r in rows] == ["config", "config"], rows
        return rows[0].error or ""

    async def fastquery_import() -> str:
        from bigfix_remote_client_relevance import TransportFastQuery

        return TransportFastQuery.__module__

    for name, fn in [
        ("versions", v),
        ("analyzer", analyze),
        ("app data dir", data_dir),
        ("keychain", keychain),
        ("TLS + certifi", tls),
        ("evaluate (no connections)", evaluate_unconfigured),
        ("fast query import", fastquery_import),
    ]:
        await step(name, fn)
    passed = sum(line.startswith("PASS") for line in lines)
    say(f"SMOKE {passed}/{len(lines)} passed")
    return lines
