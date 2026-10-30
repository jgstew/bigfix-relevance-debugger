"""A JSON-string facade for non-Python hosts (the SwiftUI shell over CPython).

Every function takes and returns ``str`` so the C API glue on the Swift side
is one call shape: ``PyObject_CallFunction(fn, "s", json)`` -> UTF-8 string.
Functions are synchronous; the host calls them off its main thread.
``evaluate`` runs its own event loop for the duration of the call.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import traceback

from bfrd_mobile_core import analysis, versions
from bfrd_mobile_core.connections import Connection, ConnectionStore
from bfrd_mobile_core.evaluate import Evaluator
from bfrd_mobile_core.secrets import default_secret_store

log = logging.getLogger(__name__)

_state: dict[str, object] = {}


def _evaluator() -> Evaluator:
    if "evaluator" not in _state:
        _state["evaluator"] = Evaluator(default_secret_store())  # no trust_prompt: two-step
    return _state["evaluator"]  # type: ignore[return-value]


def _guard(fn):
    def wrapper(arg: str = "{}") -> str:
        try:
            return json.dumps(fn(json.loads(arg) if arg else {}))
        except Exception as exc:  # the host gets the error, never a crash
            log.exception("bridge call failed")
            return json.dumps(
                {"error": f"{type(exc).__name__}: {exc}", "traceback": traceback.format_exc()}
            )

    wrapper.__name__ = fn.__name__
    return wrapper


@_guard
def info(_args: dict) -> dict:
    return versions() | {"secrets": _evaluator().secrets.name, "version": sys.version}


@_guard
def analyze(args: dict) -> dict:
    return analysis.analyze(args.get("text", "")).to_dict() | {
        "summary": analysis.analyze(args.get("text", "")).summary
    }


@_guard
def connections(_args: dict) -> dict:
    ev = _evaluator()
    return {
        "connections": [
            c.__dict__ | {"has_secret": ev.secrets.get(c.secret_account) is not None}
            for c in ConnectionStore().load()
        ]
    }


@_guard
def save_connection(args: dict) -> dict:
    password = args.pop("password", None)
    conn = Connection(**args["connection"])
    ConnectionStore().upsert(conn)
    if password:
        _evaluator().secrets.set(conn.secret_account, password)
    return {"ok": True}


@_guard
def delete_connection(args: dict) -> dict:
    store = ConnectionStore()
    for conn in store.load():
        if conn.name == args["name"]:
            _evaluator().secrets.delete(conn.secret_account)
    store.remove(args["name"])
    return {"ok": True}


@_guard
def evaluate(args: dict) -> dict:
    ev = _evaluator()
    text = args["text"]
    ev.totp_code = args.get("totp") or None
    dialects = tuple(args.get("dialects") or analysis.analyze(text).dialects)
    conns = ConnectionStore().load()

    async def run() -> list[dict]:
        return [
            r.to_dict()
            async for r in ev.run(text, dialects, conns, timeout=float(args.get("timeout", 60)))
        ]

    rows = asyncio.run(run())
    pending = [
        {"host_port": i.host_port, "fingerprint": i.fingerprint} for i in ev.pending_certs.values()
    ]
    return {"rows": rows, "pending_certs": pending}


@_guard
def trust_cert(args: dict) -> dict:
    _evaluator().trust(args["host_port"])
    return {"ok": True}


@_guard
def smoke(_args: dict) -> dict:
    from bfrd_mobile_core import smoke as smoke_module

    return {"lines": asyncio.run(smoke_module.run())}
