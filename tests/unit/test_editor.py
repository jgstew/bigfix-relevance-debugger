"""Tests for editor.py: the CodeMirror element wired to the analyzer's language server.

The browser half (relevance_codemirror.js and upstream/web/relevance_editor.js)
can't run here, so these pin the Python half of the transport: what reaches
the browser through run_method, and what the element does with an "lsp"
event from it. Each test runs the real language server in-process.
"""

from __future__ import annotations

import asyncio
import time

import pytest
from nicegui import ui
from nicegui.testing.user_simulation import user_simulation

from bigfix_relevance_debugger.editor import ALLOWED_REQUESTS, RelevanceEditor
from bigfix_relevance_debugger.upstream import relevance_analyzer


def page() -> None:
    RelevanceEditor(line_wrapping=True).mark("editor")


@pytest.fixture
async def user():
    async with user_simulation(root=page) as simulated:
        yield simulated


async def opened(user) -> tuple[RelevanceEditor, list[tuple]]:
    """The page's editor, with its run_method calls recorded instead of sent."""
    await user.open("/")
    [editor] = user.find(marker="editor").elements
    assert isinstance(editor, RelevanceEditor)
    assert isinstance(editor, ui.codemirror)  # every ui.codemirror feature still applies
    calls: list[tuple] = []
    editor.run_method = lambda name, *args, **_: calls.append((name, *args))  # type: ignore[method-assign]
    return editor, calls


async def until(predicate, timeout: float = 2.0) -> None:
    for _ in range(int(timeout / 0.02)):
        if predicate():
            return
        await asyncio.sleep(0.02)
    assert predicate()


def notifications(calls):
    return [c for c in calls if c[0] == "lspNotify"]


# --- diagnostics ------------------------------------------------------------------


async def test_publish_pushes_the_servers_diagnostics_for_the_text(user):
    editor, calls = await opened(user)
    editor.set_value("nme of operating system")

    await editor.publish()

    [(_, method, params)] = notifications(calls)
    assert method == "textDocument/publishDiagnostics"
    assert params["text"] == "nme of operating system"
    [diagnostic] = params["diagnostics"]
    assert diagnostic["code"] == "unknown-inspector"
    assert diagnostic["range"]["start"] == {"line": 0, "character": 0}


async def test_publish_without_a_change_sends_nothing(user):
    editor, calls = await opened(user)
    editor.set_value("nme of operating system")
    await editor.publish()

    await editor.publish()

    assert len(notifications(calls)) == 1


async def test_parse_errors_arrive_as_diagnostics(user):
    editor, calls = await opened(user)
    editor.set_value("name of ) x")

    await editor.publish()

    [(_, _, params)] = notifications(calls)
    assert [d["code"] for d in params["diagnostics"]] == ["parse-error"]


async def test_a_new_dialect_starts_a_new_session_and_republishes(user):
    editor, calls = await opened(user)
    editor.set_value("name of computer")
    await editor.publish("client")
    assert editor.dialect == "client"

    await editor.publish("session")

    assert editor.dialect == "session"
    assert len(notifications(calls)) == 2


async def test_a_cancelled_publish_is_resent(user, monkeypatch):
    # The worker thread finishes the sync even when publish() is cancelled;
    # the browser never got those diagnostics, so the next publish sends them.
    editor, calls = await opened(user)
    real_sync = relevance_analyzer.StatementSession.sync

    def slow_sync(self, text):
        time.sleep(0.2)
        return real_sync(self, text)

    monkeypatch.setattr(relevance_analyzer.StatementSession, "sync", slow_sync)
    editor.set_value("nme of operating system")
    task = asyncio.create_task(editor.publish())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0.3)  # the worker thread still finishes the sync

    await editor.publish()

    [(_, _, params)] = notifications(calls)
    assert params["text"] == "nme of operating system"
    assert params["diagnostics"]


async def test_a_request_does_not_swallow_the_next_publish(user):
    editor, calls = await opened(user)
    editor.set_value("nme of operating system")
    request(user, 1, "textDocument/hover", {"position": {"line": 0, "character": 1}})
    await until(lambda: any(c[0] == "lspReply" for c in calls))

    await editor.publish()

    [(_, _, params)] = notifications(calls)
    assert [d["code"] for d in params["diagnostics"]] == ["unknown-inspector"]


async def test_a_mounted_browser_gets_the_diagnostics_again(user):
    # A remounted editor starts with no diagnostics; it says so with lspReady.
    editor, calls = await opened(user)
    editor.set_value("nme of operating system")
    await editor.publish()

    user.find(marker="editor").trigger("lspReady")

    await until(lambda: len(notifications(calls)) == 2)
    assert notifications(calls)[0] == notifications(calls)[1]


# --- requests from the browser ----------------------------------------------------


def request(user, id_, method, params):
    user.find(marker="editor").trigger("lsp", {"id": id_, "method": method, "params": params})


async def test_a_request_is_answered_against_the_current_text(user):
    editor, calls = await opened(user)
    editor.set_value("name of operating system")

    request(user, 7, "textDocument/hover", {"position": {"line": 0, "character": 12}})

    await until(lambda: any(c[0] == "lspReply" for c in calls))
    [(_, id_, result, error)] = [c for c in calls if c[0] == "lspReply"]
    assert id_ == 7
    assert error is None
    assert "operating system" in result["contents"]["value"]


async def test_completion_is_allowed(user):
    editor, calls = await opened(user)
    editor.set_value("name of ")

    request(user, 1, "textDocument/completion", {"position": {"line": 0, "character": 8}})

    await until(lambda: any(c[0] == "lspReply" for c in calls))
    [(_, _, result, error)] = [c for c in calls if c[0] == "lspReply"]
    assert error is None
    assert "operating system" in [item["label"] for item in result["items"]]


@pytest.mark.parametrize("method", ["shutdown", "exit", "initialize", "textDocument/didClose"])
async def test_only_editor_requests_reach_the_server(user, method):
    # The browser is untrusted input: it must not be able to shut the server
    # down or swap its document.
    assert method not in ALLOWED_REQUESTS
    editor, calls = await opened(user)

    request(user, 3, method, {})

    await until(lambda: any(c[0] == "lspReply" for c in calls))
    [(_, id_, result, error)] = [c for c in calls if c[0] == "lspReply"]
    assert (id_, result) == (3, None)
    assert error["code"] == -32601
    editor.set_value("nme")
    await editor.publish()  # the session still works
    assert notifications(calls)


@pytest.mark.parametrize(
    "payload",
    [
        {"method": "textDocument/hover", "params": {}},  # no id
        {"id": "x", "method": "textDocument/hover", "params": {}},  # id not an int
        {"id": 4, "method": "textDocument/hover", "params": "nope"},  # params not an object
        "garbage",
    ],
)
async def test_malformed_requests_are_ignored(user, payload):
    _, calls = await opened(user)

    user.find(marker="editor").trigger("lsp", payload)
    await asyncio.sleep(0.1)

    assert [c for c in calls if c[0] == "lspReply" and c[1] != 4] == []


async def test_a_server_error_is_replied_not_raised(user):
    editor, calls = await opened(user)
    editor.set_value("now")

    # Missing position: the server answers with an error.
    request(user, 5, "textDocument/hover", {})

    await until(lambda: any(c[0] == "lspReply" for c in calls))
    [(_, id_, result, error)] = [c for c in calls if c[0] == "lspReply"]
    assert (id_, result) == (5, None)
    assert error is not None and "code" in error


# --- lifecycle --------------------------------------------------------------------


async def test_closing_during_a_dialect_switch_is_quiet(user):
    editor, calls = await opened(user)
    editor.set_value("name of operating system")
    task = asyncio.create_task(editor.publish("session"))
    await asyncio.sleep(0)  # inside the lock, awaiting a worker thread

    editor.close()

    await task  # no AttributeError
    assert notifications(calls) == []


async def test_a_request_after_close_is_answered(user):
    # Every request settles, so the browser never waits forever.
    editor, calls = await opened(user)
    editor.close()

    request(user, 9, "textDocument/hover", {"position": {"line": 0, "character": 0}})

    await until(lambda: any(c[0] == "lspReply" for c in calls))
    [(_, id_, result, error)] = [c for c in calls if c[0] == "lspReply"]
    assert (id_, result) == (9, None)
    assert error["code"] == -32800  # RequestCancelled


async def test_close_shuts_the_server_down_without_racing_a_request(user, monkeypatch):
    editor, _ = await opened(user)
    editor.set_value("name of operating system")
    session = editor._session
    assert session is not None
    real_request = relevance_analyzer.StatementSession.request
    closed_while_busy: list[bool] = []

    def slow_request(self, method, params, text):
        time.sleep(0.2)
        closed_while_busy.append(self._closed)
        return real_request(self, method, params, text)

    monkeypatch.setattr(relevance_analyzer.StatementSession, "request", slow_request)
    request(user, 2, "textDocument/hover", {"position": {"line": 0, "character": 12}})
    await asyncio.sleep(0.05)  # the request is in its worker thread

    editor.close()

    await until(lambda: session._closed)
    assert closed_while_busy == [False]  # the shutdown waited for the request


async def test_closed_editor_publishes_nothing(user):
    editor, calls = await opened(user)
    editor.set_value("nme")

    editor.close()
    await editor.publish()

    assert notifications(calls) == []


# --- what the browser half relies on ----------------------------------------------


def test_the_files_the_browser_imports_exist():
    """A NiceGUI upgrade that moves its CodeMirror component, or drops the
    hook we extend, should fail here, not silently in the browser."""
    import bigfix_relevance_debugger.editor as editor_module

    base = editor_module._NICEGUI_CODEMIRROR / "codemirror.js"
    assert "setupExtensions()" in base.read_text(encoding="utf-8")
    assert (editor_module._UPSTREAM_WEB / "relevance_editor.js").is_file()
    component = editor_module.Path(editor_module.__file__).with_name("relevance_codemirror.js")
    source = component.read_text(encoding="utf-8")
    assert 'from "nicegui-codemirror-base/codemirror.js"' in source
    assert 'from "bigfix-relevance-editor/relevance_editor.js"' in source


def test_the_shared_client_imports_nothing():
    """It must run on whatever CodeMirror the page passes in (analyzer#142)."""
    import bigfix_relevance_debugger.editor as editor_module

    source = (editor_module._UPSTREAM_WEB / "relevance_editor.js").read_text(encoding="utf-8")
    assert not [line for line in source.splitlines() if line.startswith("import ")]


def test_the_host_keeps_tooltips_below_the_page_header():
    # The shared client uses CodeMirror's default tooltip placement; the page
    # tells CodeMirror its fixed header covers the top of the window.
    import bigfix_relevance_debugger.editor as editor_module

    component = editor_module.Path(editor_module.__file__).with_name("relevance_codemirror.js")
    assert "CM.tooltips({" in component.read_text(encoding="utf-8")
    bundle = editor_module._NICEGUI_CODEMIRROR / "dist" / "index.js"
    assert " as tooltips" in bundle.read_text(encoding="utf-8")


def test_node_loads_the_shared_client_as_a_module_on_any_version():
    # Without "type": "module", Node before 22.7 / 20.19 reads a .js file as
    # CommonJS and fails on `export`.
    import json

    import bigfix_relevance_debugger.editor as editor_module

    package = json.loads((editor_module._UPSTREAM_WEB / "package.json").read_text(encoding="utf-8"))
    assert package["type"] == "module"
