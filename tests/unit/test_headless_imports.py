"""The headless modules must import without NiceGUI or the compiled/engine deps,
because the iOS app ships them without those (pyproject.toml markers).

The desktop UI (``app``) must say so clearly when NiceGUI is missing.
"""

from __future__ import annotations

import builtins
import importlib
import sys

import pytest

BLOCKED = (
    "nicegui",
    "webview",
    "uvicorn",
    "fastapi",
    "asyncssh",
    "cryptography",
    "docker",
    "zstandard",
)
HEADLESS = (
    "bigfix_relevance_debugger",
    "bigfix_relevance_debugger.analysis",
    "bigfix_relevance_debugger.evaluation",
    "bigfix_relevance_debugger.log_bridge",
    "bigfix_relevance_debugger.connections",
    "bigfix_relevance_debugger.bridge",
    "bigfix_relevance_debugger.selfcheck",
)


@pytest.fixture
def blocked(monkeypatch):
    """Import as on iOS: the blocked packages raise ImportError, and every
    module comes in fresh. ``sys.modules`` is put back exactly afterwards, so
    no test later gets a module bound to these fresh copies (which the
    autouse fixtures in conftest.py never patched)."""
    saved = dict(sys.modules)
    real_import = builtins.__import__

    def guarded(name, globals=None, locals=None, fromlist=(), level=0):
        if level == 0 and name.split(".")[0] in BLOCKED:
            raise ImportError(f"{name} is blocked (not on iOS)")
        return real_import(name, globals, locals, fromlist, level)

    for mod in list(sys.modules):
        # The libraries too: one that started importing a blocked package at
        # import time must fail here, not hide behind its cached copy.
        if mod.split(".")[0].startswith("bigfix_") or mod.split(".")[0] in BLOCKED:
            del sys.modules[mod]
    monkeypatch.setattr(builtins, "__import__", guarded)
    yield
    monkeypatch.setattr(builtins, "__import__", real_import)
    sys.modules.clear()
    sys.modules.update(saved)


@pytest.mark.parametrize("module", HEADLESS)
def test_imports_without_desktop_or_compiled_deps(module, blocked):
    importlib.import_module(module)


def test_the_guard_blocks(blocked):
    with pytest.raises(ImportError, match="blocked"):
        importlib.import_module("nicegui")


def test_the_libraries_are_imported_fresh_too(blocked):
    assert not [m for m in sys.modules if m.startswith("bigfix_")]


def test_the_desktop_app_says_it_needs_nicegui(blocked):
    with pytest.raises(ImportError, match="desktop UI and needs NiceGUI") as raised:
        importlib.import_module("bigfix_relevance_debugger.app")

    assert "bigfix_relevance_debugger.bridge" in str(raised.value)
