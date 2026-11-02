"""The versioned JSON API every non-Python host calls (the SwiftUI app; Android later).

One entry point, ``call(method, args_json) -> str``: JSON in, JSON out, and it
never raises. The reply is ``{"version": 1, "ok": true, "result": ...}`` or
``{"version": 1, "ok": false, "error": {"type": ..., "message": ...}}``.

Methods (args -> result):

* ``info`` {} -> python, platform, versions, secrets (the store's name), data_dir
* ``analyze`` {text, mode?} -> the ``AnalysisView`` + ``highlight``: [{start, end, kind}]
* ``connections`` {} -> connections, has_secret {name: bool}, backends
* ``save_connection`` {connection, password?} -> {}
* ``delete_connection`` {name} -> {}
* ``saas_sign_in`` {host, email, password, totp?} -> servers, connections, error
* ``start_run`` {text, mode?, dialects?, timeout_s?, totp?} -> run_id
* ``poll_run`` {run_id, wait_ms?} -> rows (new ones only), done, cancelled, error,
  pending_certs [{host_port, sha256, fingerprint}], totp_needed, tally
* ``cancel_run`` {run_id} -> {}
* ``trust_cert`` {host_port, sha256} -> {} (only the certificate the user was shown)
* ``selfcheck`` {} -> lines, passed, skipped, total

Built only on ``analysis``, ``evaluation`` and ``connections``: no BigFix logic
of its own. Streaming without callbacks across the C boundary: the bridge owns
one asyncio loop on a background thread, ``start_run`` schedules the run on
it, and ``poll_run`` long-polls the run's rows. The same loop keeps one
session client alive across runs (one SaaS sign-in, discovered ids kept).
Certificates are never trusted silently: a first-use certificate is refused
and listed in ``pending_certs`` until ``trust_cert``.
"""

from __future__ import annotations

import asyncio
import dataclasses
import inspect
import json
import logging
import math
import os
import sys
import threading
import uuid
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass, field
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as dist_version
from pathlib import Path
from typing import Any

import httpx
from bigfix_relevance_analyzer import highlight
from bigfix_remote_client_relevance import SessionContext
from bigfix_remote_session_relevance import (
    AsyncSessionRelevanceClient,
    ConfigError,
    ConnectionConfig,
    KnownCerts,
    MemorySecretStore,
    SaasSessionPool,
    SecretStore,
    TrustStore,
    default_secret_store,
    saas_sign_in,
)

from bigfix_relevance_debugger import selfcheck
from bigfix_relevance_debugger.analysis import AUTO, CLIENT, DIALECT_MODES, SESSION, summarize
from bigfix_relevance_debugger.connections import (
    BACKENDS,
    AppConnections,
    Connection,
    ConnectionsError,
    app_data_dir,
)
from bigfix_relevance_debugger.evaluation import (
    NO_SESSION_CONFIG,
    AnswerTally,
    EvalOptions,
    ResultRow,
    SessionClients,
    SessionEvaluation,
    default_session_client,
    session_row_wanted,
    stream_results,
    stream_session,
)

logger = logging.getLogger(__name__)

BRIDGE_VERSION = 1

TransportFactory = Callable[[ConnectionConfig], httpx.AsyncBaseTransport | None]

MAX_WAIT_MS = 60_000
"""Longest a ``poll_run`` waits for news."""

NO_FASTQUERY = (
    "No connection has Fast Query computers, so client relevance has nowhere to run. "
    "Add computer ids or a computer query to a REST or BigFix SaaS connection."
)
FASTQUERY_HOST = "Fast Query"
NO_SERVER = (
    "No BigFix server connection is saved, so session relevance has nowhere to run. "
    "Add a BigFix SaaS, REST, Explorer or Web Reports connection."
)


class BridgeError(Exception):
    """A failure with a ``type`` the host can switch on; the message is user-facing."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


class TotpNeeded(RuntimeError):
    """A SaaS account asked for a TOTP code the run doesn't have (any more)."""


def _bad(message: str) -> BridgeError:
    return BridgeError("bad_request", message)


# --- args -----------------------------------------------------------------------------


def _text(args: dict[str, Any], key: str) -> str:
    value = args.get(key)
    if not isinstance(value, str):
        raise _bad(f"{key} is required and must be text")
    return value


def _optional_text(args: dict[str, Any], key: str) -> str | None:
    value = args.get(key)
    if value is not None and not isinstance(value, str):
        raise _bad(f"{key} must be text")
    return value or None


def _number(args: dict[str, Any], key: str, default: float) -> float:
    value = args.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        raise _bad(f"{key} must be a finite number")
    return float(value)


def _mode(args: dict[str, Any]) -> str:
    mode = _optional_text(args, "mode") or AUTO
    if mode not in DIALECT_MODES:
        raise _bad(f"mode must be one of {', '.join(DIALECT_MODES)}")
    return mode


def _parse(args_json: object) -> dict[str, Any]:
    if not isinstance(args_json, str | bytes):
        raise _bad("args must be a JSON object")
    try:
        args = json.loads(args_json)
    except ValueError as exc:
        raise _bad(f"args are not JSON: {exc}") from exc
    if not isinstance(args, dict):
        raise _bad("args must be a JSON object")
    return args


# --- replies ---------------------------------------------------------------------------


def _error_type(exc: BaseException) -> str:
    if isinstance(exc, BridgeError):
        return exc.kind
    if isinstance(exc, ConnectionsError):
        return "connections"
    if isinstance(exc, ConfigError):
        return "config"
    return type(exc).__name__


def _reply(work: Callable[[], Any]) -> str:
    """Run ``work`` and wrap its result, or whatever it raised, in the envelope."""
    try:
        reply: dict[str, Any] = {"version": BRIDGE_VERSION, "ok": True, "result": work()}
    except Exception as exc:  # the contract: calls never raise
        if not isinstance(exc, BridgeError | ConnectionsError | ConfigError):
            logger.debug("bridge call failed", exc_info=True)
        reply = {
            "version": BRIDGE_VERSION,
            "ok": False,
            "error": {"type": _error_type(exc), "message": str(exc)},
        }
    try:
        return json.dumps(reply)
    except (TypeError, ValueError) as exc:  # a result that isn't JSON: a bug, still a reply
        return json.dumps(
            {
                "version": BRIDGE_VERSION,
                "ok": False,
                "error": {"type": "bad_result", "message": str(exc)},
            }
        )


def _row(row: ResultRow, *, connection: str | None = None, error_code: str | None = None) -> dict:
    data = {f.name: getattr(row, f.name) for f in dataclasses.fields(ResultRow)}
    data["answers"] = list(row.answers)
    data["connection"] = connection
    data["error_code"] = error_code
    return data


def _tally(tally: AnswerTally) -> dict[str, Any]:
    shown, more = tally.rows()
    return {
        "systems": tally.systems,
        "rows": [
            {"key": r.key, "kind": r.kind, "label": r.label, "systems": r.systems} for r in shown
        ],
        "more": more,
    }


# --- runs ------------------------------------------------------------------------------


class _OneCode:
    """The session library's prompt for a run that brought a TOTP code.

    Answers the first TOTP challenge with the code; a later one (the code is
    spent) raises :class:`TotpNeeded` rather than resend a code the service
    would count as a failed login. A password prompt gets no answer (None),
    as with no prompt: passwords come from the ``SecretStore``.
    """

    def __init__(self, code: str) -> None:
        self.code: str | None = code

    def __call__(self, text: str) -> str:
        if not text.lstrip().upper().startswith("TOTP"):
            return None  # type: ignore[return-value]  # no password to give: like no prompt
        code, self.code = self.code, None
        if code is None:
            raise TotpNeeded("the TOTP code was already used; run again with a new one")
        return code


@dataclass
class _Run:
    id: str
    tally: AnswerTally = field(default_factory=AnswerTally)
    rows: list[dict[str, Any]] = field(default_factory=list)
    sent: int = 0
    done: bool = False
    cancelled: bool = False
    error: str | None = None
    totp_needed: bool = False
    changed: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task[None] | None = None

    def fail(self, message: str) -> None:
        self.error = message if self.error is None else f"{self.error}; {message}"
        self.changed.set()

    def add(self, row: ResultRow, *, connection: str | None = None, code: str | None = None):
        self.rows.append(_row(row, connection=connection, error_code=code))
        if row.status != "config":  # how to add a server, not a system's answer
            self.tally.add(row)
        self.changed.set()


# --- the bridge ------------------------------------------------------------------------


class Bridge:
    """The state behind ``call``: the app's connections, trust, one loop, the runs.

    ``transport_factory`` gives each connection an httpx transport (tests and
    proxies; it bypasses certificate pinning, which applies only to the
    session library's own TLS). ``selfcheck_network`` off skips the
    self-check's TLS step.
    """

    def __init__(
        self,
        *,
        data_dir: Path,
        secrets: SecretStore,
        transport_factory: TransportFactory | None = None,
        selfcheck_network: bool = True,
    ) -> None:
        self.data_dir = data_dir
        self.secrets = secrets
        self.connections = AppConnections(data_dir / "connections.json", secrets)
        self.trust = TrustStore(KnownCerts(data_dir / "known_certs.toml"), trust_on_first_use=False)
        self._transport_factory = transport_factory
        self._selfcheck_network = selfcheck_network
        self._clients = SessionClients(self._session_client)
        self._next_prompt: _OneCode | None = None
        self._saas_pool: SaasSessionPool | None = None
        self._runs: dict[str, _Run] = {}
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None

    # -- dispatch ---------------------------------------------------------------------

    def call(self, method: str, args_json: str) -> str:
        return _reply(lambda: self.handle(method, _parse(args_json)))

    def handle(self, method: str, args: dict[str, Any]) -> Any:
        handler = getattr(self, f"_m_{_method(method)}")
        if inspect.iscoroutinefunction(handler):
            return self._await(handler(args))
        return handler(args)

    # -- the loop ---------------------------------------------------------------------

    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        with self._lock:
            if self._loop is None:
                loop = asyncio.new_event_loop()
                thread = threading.Thread(
                    target=loop.run_forever, name="bigfix-relevance-bridge", daemon=True
                )
                thread.start()
                self._loop, self._thread = loop, thread
            return self._loop

    def _await(self, work: Coroutine[Any, Any, Any]) -> Any:
        return asyncio.run_coroutine_threadsafe(work, self._ensure_loop()).result()

    def close(self) -> None:
        """Cancel the runs, close the session clients and stop the loop thread."""
        with self._lock:
            loop, thread = self._loop, self._thread
            self._loop = self._thread = None
        if loop is None or thread is None:
            return

        async def shut() -> None:
            tasks = [r.task for r in self._runs.values() if r.task is not None]
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            self._runs.clear()
            await self._clients.aclose()
            self._saas_pool = None  # its sessions belong to this loop

        try:
            asyncio.run_coroutine_threadsafe(shut(), loop).result(10)
        finally:
            loop.call_soon_threadsafe(loop.stop)
            thread.join(10)
            loop.close()

    # -- session clients --------------------------------------------------------------

    def _session_client(self, opts: EvalOptions) -> AsyncSessionRelevanceClient:
        prompt, self._next_prompt = self._next_prompt, None
        return default_session_client(
            opts,
            source=self.connections,
            trust=self.trust,
            prompt=prompt,
            transport_factory=self._transport_factory,
        )

    def _pool(self) -> SaasSessionPool:
        # Created on the loop: a SaaS session belongs to the loop it signed in on.
        if self._saas_pool is None:
            self._saas_pool = SaasSessionPool()
        return self._saas_pool

    # -- methods: sync, on the caller's thread ----------------------------------------

    def _m_info(self, args: dict[str, Any]) -> dict[str, Any]:
        versions = {}
        for dist in (
            "bigfix-relevance-debugger",
            "bigfix-relevance-analyzer",
            "bigfix-remote-client-relevance",
            "bigfix-remote-session-relevance",
        ):
            try:
                versions[dist] = dist_version(dist)
            except PackageNotFoundError:
                versions[dist] = None
        return {
            "bridge_version": BRIDGE_VERSION,
            "python": sys.version,
            "platform": sys.platform,
            "versions": versions,
            "secrets": self.secrets.name,
            "data_dir": str(self.data_dir),
        }

    def _m_analyze(self, args: dict[str, Any]) -> dict[str, Any]:
        text = _text(args, "text")
        view = summarize(text, _mode(args))
        result = dataclasses.asdict(view)
        result["dialect"] = view.dialect
        result["highlight"] = [
            {"start": s.start, "end": s.end, "kind": str(s.kind)} for s in highlight(text)
        ]
        return result

    # -- methods: on the loop ---------------------------------------------------------

    async def _m_connections(self, args: dict[str, Any]) -> dict[str, Any]:
        items = self.connections.load()
        return {
            "connections": [c.to_dict() for c in items],
            "has_secret": {c.name: self.connections.has_secret(c) for c in items},
            "backends": BACKENDS,
        }

    async def _m_save_connection(self, args: dict[str, Any]) -> dict[str, Any]:
        conn = Connection.from_dict(args.get("connection"))
        password = _optional_text(args, "password")
        self.connections.upsert(conn, password=password)
        self._clients.reset()
        return {}

    async def _m_delete_connection(self, args: dict[str, Any]) -> dict[str, Any]:
        self.connections.remove(_text(args, "name"))
        self._clients.reset()
        return {}

    async def _m_saas_sign_in(self, args: dict[str, Any]) -> dict[str, Any]:
        host, email, password = _text(args, "host"), _text(args, "email"), _text(args, "password")
        transport = None
        if self._transport_factory is not None:
            probe = ConnectionConfig(name="sign-in", backend="saas", server=host, username=email)
            transport = self._transport_factory(probe)
        account = await saas_sign_in(
            host,
            email,
            password,
            totp=_optional_text(args, "totp"),
            pool=self._pool(),
            transport=transport,
        )
        error = account.error
        return {
            "servers": list(account.servers),
            "connections": [
                Connection(
                    name=c.name,
                    backend=c.backend,
                    server=c.server,
                    username=c.username or "",
                    sid=c.sid,
                ).to_dict()
                for c in account.connections()
            ],
            "error": None
            if error is None
            else {"kind": error.kind, "code": error.code, "message": error.message},
        }

    async def _m_start_run(self, args: dict[str, Any]) -> dict[str, Any]:
        text = _text(args, "text")
        mode = _mode(args)
        timeout_s = _number(args, "timeout_s", EvalOptions().timeout_s)
        if not timeout_s > 0:
            raise _bad("timeout_s must be greater than 0")
        totp = _optional_text(args, "totp")
        chosen = args.get("dialects")
        if chosen is not None:
            if not isinstance(chosen, list) or not chosen or not set(chosen) <= {SESSION, CLIENT}:
                raise _bad('dialects must be a list of "session" and/or "client"')
            dialects = tuple(d for d in (SESSION, CLIENT) if d in chosen)
            auto_both = False
        else:
            dialects = summarize(text, mode).run_dialects
            auto_both = mode == AUTO and dialects == (SESSION, CLIENT)

        code = _OneCode(totp) if totp else None
        if code is not None:
            # A remembered totp_required in the old pool would turn the code
            # away unsent: Fast Query never retries a stored sign-in failure.
            self._saas_pool = SaasSessionPool()
        if code is not None and SESSION in dialects:
            # A fresh client, built with the code; it then keeps the SaaS session.
            self._next_prompt = code
            self._clients.reset()

        run = _Run(uuid.uuid4().hex)
        run.task = asyncio.get_running_loop().create_task(
            self._drive(
                run,
                text,
                dialects,
                EvalOptions(timeout_s=timeout_s),
                auto_both=auto_both,
                fastquery_prompt=code if SESSION not in dialects else None,
            )
        )
        self._runs[run.id] = run
        return {"run_id": run.id}

    async def _drive(
        self,
        run: _Run,
        text: str,
        dialects: tuple[str, ...],
        opts: EvalOptions,
        *,
        auto_both: bool,
        fastquery_prompt: _OneCode | None,
    ) -> None:
        async def session(optional: bool) -> None:
            async for ev in stream_session(text, opts, clients=self._clients):
                if session_row_wanted(ev, optional=optional):
                    self._add_session(run, ev)

        async def client(targets: list, have_servers: bool) -> None:
            if not targets:
                if not (auto_both and have_servers):
                    run.add(_no_fastquery_row())
                return
            context = SessionContext(
                config=self.connections.config(),
                trust=self.trust,
                prompt=fastquery_prompt,
                saas_sessions=self._pool(),
                http_transport=self._transport_factory,
            )
            async for result in stream_results(text, targets, opts, session=context):
                if _asks_for_totp(result.error):
                    run.totp_needed = True
                run.add(ResultRow.from_result(result))

        async def guarded(part: Awaitable[None]) -> None:
            # One half failing must not stop the other: it becomes the run's error.
            try:
                await part
            except Exception as exc:
                logger.debug("bridge run part failed", exc_info=True)
                run.fail(f"{type(exc).__name__}: {exc}")

        try:
            targets = self.connections.fastquery_targets() if CLIENT in dialects else []
            have_servers = bool(self.connections.load()) if auto_both else False
            parts: list[Awaitable[None]] = []
            if SESSION in dialects:
                # Auto added session beside client: no server is worth a row
                # only when client relevance has nowhere to run either.
                parts.append(guarded(session(auto_both and bool(targets))))
            if CLIENT in dialects:
                parts.append(guarded(client(targets, have_servers)))
            await asyncio.gather(*parts)
        except asyncio.CancelledError:
            run.cancelled = True
        except Exception as exc:
            logger.debug("bridge run failed", exc_info=True)
            run.fail(f"{type(exc).__name__}: {exc}")
        finally:
            run.done = True
            run.changed.set()

    def _add_session(self, run: _Run, ev: SessionEvaluation) -> None:
        if ev.error_kind == "config" and ev.error == NO_SESSION_CONFIG:
            ev = dataclasses.replace(ev, error=NO_SERVER)  # the CLI's advice doesn't apply
        if ev.error_code == "totp_required" or (ev.error or "").startswith(TotpNeeded.__name__):
            run.totp_needed = True
        run.add(ResultRow.from_session(ev), connection=ev.connection, code=ev.error_code)

    def _run(self, args: dict[str, Any]) -> _Run:
        run = self._runs.get(_text(args, "run_id"))
        if run is None:
            raise BridgeError(
                "unknown_run", "no such run (finished runs are forgotten once polled)"
            )
        return run

    async def _m_poll_run(self, args: dict[str, Any]) -> dict[str, Any]:
        run = self._run(args)
        wait_ms = min(max(_number(args, "wait_ms", 0), 0), MAX_WAIT_MS)
        if run.sent == len(run.rows) and not run.done and wait_ms:
            run.changed.clear()
            try:
                await asyncio.wait_for(run.changed.wait(), wait_ms / 1000)
            except TimeoutError:
                pass
        new, run.sent = run.rows[run.sent :], len(run.rows)
        if run.done:
            self._runs.pop(run.id, None)
        return {
            "rows": new,
            "done": run.done,
            "cancelled": run.cancelled,
            "error": run.error,
            "pending_certs": [
                {"host_port": c.host_port, "sha256": c.sha256, "fingerprint": c.fingerprint}
                for c in self.trust.pending.values()
            ],
            "totp_needed": run.totp_needed,
            "tally": _tally(run.tally),
        }

    async def _m_cancel_run(self, args: dict[str, Any]) -> dict[str, Any]:
        run = self._run(args)
        if run.task is not None and not run.task.done():
            run.task.cancel()
            await asyncio.wait([run.task])
        return {}

    async def _m_trust_cert(self, args: dict[str, Any]) -> dict[str, Any]:
        """Pin the pending certificate, only if it is the one the user was shown (``sha256``)."""
        host_port, sha256 = _text(args, "host_port"), _text(args, "sha256")
        pending = self.trust.pending.get(host_port)
        if pending is None:
            raise BridgeError(
                "not_pending", f"{host_port} has no certificate waiting to be trusted"
            )
        if pending.sha256.lower() != sha256.strip().lower():
            raise BridgeError(
                "changed",
                f"{host_port} now presents a different certificate than the one shown; "
                "run again and check the new fingerprint",
            )
        try:
            self.trust.accept(host_port)
        except KeyError:
            raise BridgeError(
                "not_pending", f"{host_port} has no certificate waiting to be trusted"
            ) from None
        except ValueError as exc:
            raise BridgeError("pin_mismatch", str(exc)) from None
        # The next run connects afresh, through the new pin.
        self._clients.reset()
        return {}

    async def _m_selfcheck(self, args: dict[str, Any]) -> dict[str, Any]:
        checks = await selfcheck.run(
            data_dir=self.data_dir, secrets=self.secrets, network=self._selfcheck_network
        )
        return selfcheck.report(checks)


def _asks_for_totp(error: str | None) -> bool:
    """A Fast Query row failing on a SaaS TOTP challenge. Its error is text only
    (the session library's message), so this reads the text."""
    return error is not None and "totp code" in error.casefold()


def _no_fastquery_row() -> ResultRow:
    return ResultRow(
        status="config",
        ok=False,
        host=FASTQUERY_HOST,
        display_host=FASTQUERY_HOST,
        answers=(),
        answer_types_text="",
        qna_version_text="",
        platform_text="",
        elapsed_ms=None,
        error=NO_FASTQUERY,
        raw_qna_output="",
        dialect=CLIENT,
        detail_text="Fast Query",
    )


METHODS = frozenset(
    {
        "info",
        "analyze",
        "connections",
        "save_connection",
        "delete_connection",
        "saas_sign_in",
        "start_run",
        "poll_run",
        "cancel_run",
        "trust_cert",
        "selfcheck",
    }
)


def _method(method: object) -> str:
    if not isinstance(method, str) or method not in METHODS:
        raise BridgeError("unknown_method", f"unknown method {method!r}")
    return method


# --- the process's bridge ----------------------------------------------------------------


class _SystemSecrets:
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


_default: Bridge | None = None
_default_lock = threading.Lock()


def _bridge() -> Bridge:
    """The process's bridge, built at the first call that needs it.

    On iOS and Android the session library's own files go in the app's data
    dir too (``BFRSR_HOME``), unless the host already chose. Secrets go to the
    session library's ``default_secret_store()`` (the Keychain on iOS and
    macOS, keyring elsewhere), or stay in memory while it finds none (Android,
    or a desktop with no usable keyring), so they don't survive a restart.
    """
    global _default
    with _default_lock:
        if _default is None:
            data_dir = app_data_dir()
            if sys.platform in ("ios", "android"):
                os.environ.setdefault("BFRSR_HOME", str(data_dir / "session-relevance"))
            _default = Bridge(data_dir=data_dir, secrets=_SystemSecrets())
        return _default


def call(method: str, args_json: str) -> str:
    """Dispatch one call: ``{"version": 1, "ok": true, "result": …}`` or
    ``{"version": 1, "ok": false, "error": {"type", "message"}}``. Never raises."""

    def work() -> Any:
        name = _method(method)
        args = _parse(args_json)
        return _bridge().handle(name, args)

    return _reply(work)
