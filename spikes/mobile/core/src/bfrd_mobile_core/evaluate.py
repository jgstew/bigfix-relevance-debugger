"""Evaluation: session relevance on the server, client relevance via Fast Query.

Async, for the UI's asyncio loop. Everything is built from the app's own
:class:`~.connections.Connection` list plus secrets from a
:class:`~.secrets.SecretStore`: no config file, no ``BES_*`` env vars, no
local root server lookup (``load_config`` is never called).

TLS trust-on-first-use goes through the UI. A first-seen certificate is
*never* auto-accepted: with no UI answer the connection fails and the cert
lands in :attr:`Evaluator.pending_certs`, so a host that can't await a
dialog mid-request (the Swift bridge) can ask afterwards and call
:meth:`Evaluator.trust`, then evaluate again.
"""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import asdict, dataclass, replace

from bigfix_remote_session_relevance import (
    AsyncSessionRelevanceClient,
    CertInfo,
    Config,
    ConnectionConfig,
    KnownCerts,
    RestBackend,
    SaasBackend,
    TrustStore,
)

from bfrd_mobile_core.analysis import CLIENT, SESSION
from bfrd_mobile_core.connections import Connection, app_data_dir
from bfrd_mobile_core.secrets import SecretStore

log = logging.getLogger(__name__)

TrustPrompt = Callable[[CertInfo], Awaitable[bool]]


class TotpRequired(Exception):
    """SaaS asked for a TOTP code and the UI hasn't supplied one."""


@dataclass(frozen=True)
class ResultRow:
    dialect: str  # "session" | "client"
    connection: str
    target: str  # deployment/server for session, computer for Fast Query
    ok: bool
    answers: tuple[str, ...] = ()
    type: str | None = None
    error: str | None = None
    error_kind: str | None = None
    elapsed_ms: float | None = None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def to_connection_config(conn: Connection, secret: str | None) -> ConnectionConfig:
    """The library's config for one app connection. ``verify="tofu"`` always."""
    return ConnectionConfig(
        name=conn.name,
        backend=conn.backend,
        server=conn.server,
        username=conn.username or None,
        password=secret,
        sid=conn.sid if conn.backend == "saas" else None,
        verify="tofu",
    )


def parse_computers(spec: str) -> tuple[list[int], str | None]:
    """``"123, 456"`` -> ids; anything else is session relevance picking computers."""
    spec = spec.strip()
    if not spec:
        return [], None
    parts = [p.strip() for p in spec.replace(";", ",").split(",") if p.strip()]
    if parts and all(p.isdigit() for p in parts):
        return [int(p) for p in parts], None
    return [], spec


class Evaluator:
    """One per app. Holds the trust store and the one-shot TOTP code."""

    def __init__(
        self,
        secrets: SecretStore,
        *,
        trust_prompt: TrustPrompt | None = None,
        known_certs_path: str | None = None,
        transport_factory: Callable[[ConnectionConfig], object] | None = None,
    ) -> None:
        self.secrets = secrets
        self._transport_factory = transport_factory  # tests: httpx.MockTransport
        self.trust_prompt = trust_prompt
        self.known = KnownCerts(known_certs_path or app_data_dir() / "known_certs.toml")
        self.pending_certs: dict[str, CertInfo] = {}
        self.totp_code: str | None = None
        """Set by the UI just before a SaaS sign-in; used once, then cleared."""

    # -- trust / TOTP ------------------------------------------------------

    async def _on_untrusted(self, info: CertInfo) -> bool:
        if self.trust_prompt is not None:
            return await self.trust_prompt(info)
        self.pending_certs[info.host_port] = info
        return False  # never auto-accept: the host asks, then calls trust()

    def trust(self, host_port: str) -> None:
        info = self.pending_certs.pop(host_port)
        self.known.pin(info, trusted_by="prompt")

    def _trust_store(self) -> TrustStore:
        return TrustStore(self.known, callback=self._on_untrusted)

    def _totp(self, _message: str) -> str:
        # The library calls this synchronously from inside the sign-in
        # request, so a GUI can't open a dialog here. The UI collects the
        # code first instead. Raising (rather than returning "") means no
        # failed login is sent: SaaS locks an account after 100.
        code, self.totp_code = self.totp_code, None
        if not code:
            raise TotpRequired("this account needs a TOTP code: enter it and run again")
        return code

    def _config(self, conn: Connection) -> ConnectionConfig:
        return to_connection_config(conn, self.secrets.get(conn.secret_account))

    # -- session relevance -------------------------------------------------

    async def session(
        self,
        text: str,
        connections: list[Connection],
        *,
        timeout: float = 60.0,
        configs: list[ConnectionConfig] | None = None,
    ) -> AsyncIterator[ResultRow]:
        """``configs`` overrides ``connections`` (desktop: the CLI's own config)."""
        if configs is None:
            configs = [replace(self._config(c), read_timeout=timeout) for c in connections]
        if not configs:
            yield ResultRow(
                SESSION,
                "-",
                "-",
                False,
                error="Add a BigFix connection first.",
                error_kind="config",
            )
            return
        try:
            async with AsyncSessionRelevanceClient(
                Config(connections=tuple(configs)),
                trust=self._trust_store(),
                prompt=self._totp,
                record_deployment_ids=False,
                transport_factory=self._transport_factory,  # type: ignore[arg-type]
            ) as client:
                async for r in client.evaluate_stream(text):
                    yield ResultRow(
                        dialect=SESSION,
                        connection=r.connection or "?",
                        target=r.server or r.deployment_id or "",
                        ok=r.ok,
                        answers=tuple(r.answer_strings),
                        type=r.type,
                        error=r.error.message if r.error else None,
                        error_kind=r.error.kind if r.error else None,
                        elapsed_ms=r.elapsed_ms,
                    )
        except TotpRequired as exc:
            yield ResultRow(SESSION, "-", "-", False, error=str(exc), error_kind="totp")

    # -- client relevance via Fast Query ----------------------------------

    async def fastquery(
        self, text: str, conn: Connection, *, timeout: float = 60.0
    ) -> list[ResultRow]:
        from bigfix_remote_client_relevance import TransportFastQuery

        started = time.monotonic()

        def fail(message: str, kind: str) -> list[ResultRow]:
            elapsed = (time.monotonic() - started) * 1000
            return [
                ResultRow(
                    CLIENT,
                    conn.name,
                    "fastquery",
                    False,
                    error=message,
                    error_kind=kind,
                    elapsed_ms=elapsed,
                )
            ]

        if not conn.can_fastquery:
            return fail(
                f"Fast Query needs a SaaS or REST connection, not {conn.backend}.", "config"
            )
        ids, query = parse_computers(conn.computers)
        if not ids and not query:
            return fail(
                "Pick computers for Fast Query: ids, or `bes computers whose (...)`.", "config"
            )
        cls = SaasBackend if conn.backend == "saas" else RestBackend
        try:
            config = self._config(conn)
            backend = cls.from_config(
                config,
                trust=self._trust_store(),
                prompt=self._totp,
                transport=self._transport_factory(config) if self._transport_factory else None,  # type: ignore[arg-type]
            )
            transport = TransportFastQuery(
                backend,
                computer_ids=ids,
                computer_query=query,
                use_client_context=conn.use_client_context,
                host=conn.name,
            )
            try:
                results = await transport.evaluate_client_relevance_fanout(text, timeout_s=timeout)
            finally:
                await transport.aclose()
        except TotpRequired as exc:
            return fail(str(exc), "totp")
        return [
            ResultRow(
                dialect=CLIENT,
                connection=conn.name,
                target=r.host,
                ok=r.error is None,
                answers=tuple(r.answers),
                error=r.error,
                error_kind=r.error_kind,
                elapsed_ms=r.elapsed_ms,
            )
            for r in results
        ]

    # -- both --------------------------------------------------------------

    async def run(
        self,
        text: str,
        dialects: tuple[str, ...],
        connections: list[Connection],
        *,
        timeout: float = 60.0,
    ) -> AsyncIterator[ResultRow]:
        """What Evaluate does: session rows first, then Fast Query rows."""
        if SESSION in dialects:
            async for row in self.session(text, connections, timeout=timeout):
                yield row
        if CLIENT in dialects:
            fq = [c for c in connections if c.can_fastquery and c.computers.strip()]
            if not fq:
                yield ResultRow(
                    CLIENT,
                    "-",
                    "-",
                    False,
                    error_kind="config",
                    error=(
                        "Client relevance runs through Fast Query: give a SaaS or REST "
                        "connection some computers to target."
                    ),
                )
            for conn in fq:
                for row in await self.fastquery(text, conn, timeout=timeout):
                    yield row
