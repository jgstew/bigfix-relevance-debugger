"""Run bigfix-relevance-debugger's NiceGUI app, unchanged, inside an iOS app.

The Swift host embeds CPython (python.org's iOS XCframework), calls
``start()`` once, then points a WKWebView at ``http://127.0.0.1:<port>``.
The app itself is ``bigfix_relevance_debugger.app.main`` with the same CLI
flags a desktop user would pass: no fork, no patching.

JSON in, JSON out, like the SwiftUI spike's bridge.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import sys
import threading
import traceback
import types
from pathlib import Path

log = logging.getLogger("nicegui_ios_host")

APP_ID = "com.github.jgstew.bigfix-relevance-nicegui"
_state: dict[str, object] = {}


def _data_dir() -> Path:
    # HOME is the app's sandbox container on iOS; the bundle and "/" are read-only.
    path = Path.home() / "Library" / "Application Support" / APP_ID
    path.mkdir(parents=True, exist_ok=True)
    return path


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _stub_multiprocessing() -> None:
    """iOS has no fork/subprocesses, so CPython ships no ``_multiprocessing``.

    NiceGUI imports ``multiprocessing.forkserver`` / ``.synchronize`` at import
    time (for ``run.cpu_bound``'s process pool) and already treats a
    ``NotImplementedError`` from building the pool as "no process pool"
    (``nicegui/run.py`` ``setup()``). This stub lets the imports succeed and
    makes the pool's first semaphore raise exactly that. Upstream fix: NiceGUI
    skips the process pool where ``_multiprocessing`` is missing (iOS, Android,
    Pyodide).
    """
    if sys.platform not in ("ios", "android") or "_multiprocessing" in sys.modules:
        return
    try:
        import _multiprocessing  # noqa: F401

        return
    except ImportError:
        pass

    class SemLock:
        SEM_VALUE_MAX = 2**31 - 1

        def __init__(self, *args: object, **kwargs: object) -> None:
            raise NotImplementedError("no process pool on this platform (no _multiprocessing)")

        @staticmethod
        def _rebuild(*args: object) -> None:
            raise NotImplementedError

    def sem_unlink(name: str) -> None:
        raise NotImplementedError

    def unavailable(*args: object, **kwargs: object) -> None:
        raise NotImplementedError("no shared memory on this platform")

    stub = types.ModuleType("_multiprocessing")
    stub.SemLock = SemLock  # type: ignore[attr-defined]
    stub.sem_unlink = sem_unlink  # type: ignore[attr-defined]
    stub.flags = {}  # type: ignore[attr-defined]
    sys.modules["_multiprocessing"] = stub
    # multiprocessing.resource_tracker imports this too (shared_memory cleanup).
    shm = types.ModuleType("_posixshmem")
    shm.shm_open = unavailable  # type: ignore[attr-defined]
    shm.shm_unlink = unavailable  # type: ignore[attr-defined]
    sys.modules.setdefault("_posixshmem", shm)


def _fill_editor_on_connect(text: str) -> None:
    """Spike aid (BFRD_DEMO_TEXT): type ``text`` into the editor when a page
    connects, so the analyzer and language server can be checked in the
    simulator without taps. Never presses Evaluate."""
    from nicegui import app as nicegui_app

    from bigfix_relevance_debugger.editor import RelevanceEditor

    def fill(client) -> None:  # nicegui.Client
        for element in list(client.elements.values()):
            if isinstance(element, RelevanceEditor):
                element.value = text
                log.info("demo text set in the editor")

    nicegui_app.on_connect(fill)


def _serve(port: int) -> None:
    try:
        _stub_multiprocessing()
        from bigfix_relevance_debugger import app

        if demo := os.environ.get("BFRD_DEMO_TEXT"):
            _fill_editor_on_connect(demo)

        # Exactly what a desktop user runs, minus the browser/native window:
        #   bigfix-relevance-debugger --no-browser --port <port>
        app.main(["--no-browser", "--port", str(port)])
    except BaseException as exc:  # SystemExit included: report, don't vanish
        _state["error"] = "".join(traceback.format_exception(exc))
        log.exception("NiceGUI app stopped")


def start(_args: str = "{}") -> str:
    """Start the server on a background thread; return its port at once."""
    if "port" not in _state:
        logging.basicConfig(level=logging.INFO)
        data = _data_dir()
        # NiceGUI keeps .nicegui/ storage relative to the working directory.
        os.chdir(data)
        os.environ.setdefault("NICEGUI_STORAGE_PATH", str(data / ".nicegui"))
        port = _free_port()
        _state["port"] = port
        threading.Thread(target=_serve, args=(port,), name="nicegui", daemon=True).start()
        log.info("NiceGUI starting on http://127.0.0.1:%d", port)
    return json.dumps({"port": _state["port"]})


def status(_args: str = "{}") -> str:
    return json.dumps({"port": _state.get("port"), "error": _state.get("error")})
