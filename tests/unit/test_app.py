"""Tests for app.py: CLI entry point, debouncing, and the page via NiceGUI's simulated user.

The page tests use ``user_simulation`` (no browser, no server socket) and
replace the evaluation entry points app.py imports, so nothing reaches
Docker, qna or SSH.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from collections.abc import AsyncIterator

import pytest
from bigfix_remote_client_relevance import ClientRelevanceResult, InventoryError, Target
from nicegui import ui
from nicegui.testing.user_download import UserDownload
from nicegui.testing.user_simulation import user_simulation

from bigfix_relevance_debugger import app as app_module
from bigfix_relevance_debugger import evaluation
from bigfix_relevance_debugger.app import Debouncer, build_parser, main
from bigfix_relevance_debugger.evaluation import (
    AnswerTally,
    InventoryLoad,
    LevelResult,
    ProbeView,
    SessionEvaluation,
)

# --- CLI -------------------------------------------------------------------------


def test_parser_defaults_bind_localhost():
    args = build_parser().parse_args([])

    assert args.host == "127.0.0.1"
    assert args.port is None
    assert args.no_browser is False
    assert args.browser is False
    assert args.verbose is False


def test_parser_flags():
    args = build_parser().parse_args(
        ["--host", "0.0.0.0", "--port", "9000", "--no-browser", "--browser", "-v"]
    )

    assert args.host == "0.0.0.0"
    assert args.port == 9000
    assert args.no_browser is True
    assert args.browser is True
    assert args.verbose is True


@pytest.fixture
def captured_run(monkeypatch):
    calls: list[dict] = []

    def fake_run(root=None, **kwargs):
        calls.append({"root": root, **kwargs})

    monkeypatch.setattr(app_module.ui, "run", fake_run)
    monkeypatch.setattr(app_module, "_log_level", logging.INFO)
    # Native mode is available unless a test says otherwise.
    monkeypatch.setattr(app_module, "native_window_problem", lambda: None)
    return calls


def test_main_runs_without_reload(captured_run):
    main([])

    [call] = captured_run
    assert call["reload"] is False
    assert call["host"] == "127.0.0.1"
    assert call["root"] is app_module.root
    assert call["dark"] is None


def test_main_uses_the_logo_as_favicon(captured_run):
    main([])

    [call] = captured_run
    assert call["favicon"] == app_module.LOGO
    assert app_module.LOGO.is_file()


def test_main_defaults_to_a_native_window(captured_run):
    main([])

    [call] = captured_run
    assert call["native"] is True
    assert call["window_size"] == app_module.WINDOW_SIZE
    # Native mode finds its own free port.
    assert "port" not in call


def test_browser_flag_opens_a_tab_instead(captured_run):
    main(["--browser"])

    [call] = captured_run
    assert call["native"] is False
    assert call["show"] is True
    assert "window_size" not in call


def test_no_browser_serves_without_any_window(captured_run):
    main(["--port", "9123", "--no-browser"])

    [call] = captured_run
    assert call["native"] is False
    assert call["show"] is False
    assert call["port"] == 9123


def test_native_unavailable_falls_back_to_a_browser_tab(captured_run, monkeypatch, caplog):
    monkeypatch.setattr(app_module, "native_window_problem", lambda: "no GUI toolkit")

    with caplog.at_level(logging.WARNING):
        main([])

    [call] = captured_run
    assert call["native"] is False
    assert call["show"] is True
    assert "no GUI toolkit" in caplog.text
    assert "browser" in caplog.text


def test_main_verbose_sets_debug(captured_run):
    main(["-v"])

    assert app_module._log_level == logging.DEBUG


def test_main_default_log_level_is_info(captured_run):
    main([])

    assert app_module._log_level == logging.INFO


# --- native_window_problem ----------------------------------------------------------


@pytest.fixture
def specs(monkeypatch):
    present: set[str] = {"webview"}
    monkeypatch.setattr(
        app_module.importlib.util,
        "find_spec",
        lambda name: object() if name in present else None,
    )
    return present


def test_native_ok_on_macos_with_pywebview(monkeypatch, specs):
    monkeypatch.setattr("sys.platform", "darwin")

    assert app_module.native_window_problem() is None


def test_native_needs_pywebview(monkeypatch, specs):
    monkeypatch.setattr("sys.platform", "darwin")
    specs.clear()

    assert "pywebview" in (app_module.native_window_problem() or "")


def test_native_on_linux_needs_a_display(monkeypatch, specs):
    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    specs.add("gi")

    assert "display" in (app_module.native_window_problem() or "")


def test_native_on_linux_needs_gtk_or_qt(monkeypatch, specs):
    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.setenv("DISPLAY", ":0")

    assert "GTK" in (app_module.native_window_problem() or "")
    specs.add("qtpy")
    assert app_module.native_window_problem() is None


# --- Debouncer ---------------------------------------------------------------------


async def test_debouncer_runs_only_the_last_trigger():
    calls: list[int] = []
    debouncer = Debouncer(0.05, lambda: calls.append(1))

    for _ in range(5):
        debouncer.trigger()
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.1)

    assert calls == [1]


async def test_debouncer_cancel():
    calls: list[int] = []
    debouncer = Debouncer(0.05, lambda: calls.append(1))

    debouncer.trigger()
    debouncer.cancel()
    await asyncio.sleep(0.1)

    assert calls == []


async def test_debouncer_supports_async_callbacks():
    calls: list[int] = []

    async def callback():
        calls.append(1)

    debouncer = Debouncer(0.01, callback)
    debouncer.trigger()
    await asyncio.sleep(0.05)

    assert calls == [1]


# --- page ------------------------------------------------------------------------------


@pytest.fixture
def no_inventory(monkeypatch):
    monkeypatch.setattr(app_module, "find_inventory_path", lambda: None)
    monkeypatch.setattr(app_module, "find_or_discover_inventory", lambda: None)


@pytest.fixture
async def user(no_inventory):
    async with user_simulation(root=app_module.root) as simulated:
        yield simulated


def editor(user):
    [element] = user.find(marker="editor").elements
    assert isinstance(element, ui.codemirror)
    return element


async def set_text(user, text: str) -> None:
    editor(user).set_value(text)
    # Past the 300 ms debounce.
    await asyncio.sleep(0.4)


async def notified(user, text: str, timeout: float = 2.0) -> None:
    """Wait for a notification containing ``text``.

    Not should_see: the side panel's target count shows form problems too, so
    that would pass before the click had done anything.
    """
    for _ in range(int(timeout / 0.05)):
        if user.notify.contains(text):
            return
        await asyncio.sleep(0.05)
    assert user.notify.contains(text), user.notify.messages


async def test_page_renders(user):
    await user.open("/")

    await user.should_see("BigFix Relevance Debugger")
    await user.should_see(marker="evaluate")
    await user.should_see(marker="breakdown")


async def test_live_analysis_shows_parse_error_with_position(user):
    await user.open("/")

    await set_text(user, "name of (operating system")

    await user.should_see("line 1, column 26")
    [button] = user.find(marker="breakdown").elements
    assert not button.enabled


async def test_live_analysis_shows_lint_finding(user):
    await user.open("/")

    await set_text(user, "name of operating sytem")

    # Table cells are props, not child elements, so read the rows directly.
    [table] = user.find(marker="findings").elements
    [row] = table.rows
    assert row["code"] == "unknown-inspector"
    assert "operating system" in [f["suggestion"] for f in row["fixes"]]
    assert "suggestions" not in row  # the slot renders "fixes"; a string would be dead


def record_editor_calls(user) -> list[tuple]:
    """The editor's run_method calls (diagnostics, replies), recorded instead of sent."""
    calls: list[tuple] = []
    editor(user).run_method = lambda name, *args, **_: calls.append((name, *args))  # type: ignore[method-assign]
    return calls


def published(calls) -> list[dict]:
    return [c[2] for c in calls if c[:2] == ("lspNotify", "textDocument/publishDiagnostics")]


async def wait_for(predicate, timeout: float = 2.0) -> None:
    """The editor publishes after the debounce *and* a lint in a worker thread."""
    for _ in range(int(timeout / 0.02)):
        if predicate():
            return
        await asyncio.sleep(0.02)
    assert predicate()


async def test_live_analysis_publishes_diagnostics_to_the_editor(user):
    await user.open("/")
    calls = record_editor_calls(user)

    await set_text(user, "nme of operating sytem")

    await wait_for(lambda: published(calls))
    [params] = published(calls)
    assert params["text"] == "nme of operating sytem"
    messages = [d["message"] for d in params["diagnostics"]]
    assert len(messages) == 2
    assert "`nme`" in messages[0] and "`operating sytem`" in messages[1]
    # The squiggles come from the language server, not decorations.
    assert editor(user).decorations == []


async def test_clicking_inside_the_debounce_still_updates_the_editor(user):
    # Breakdown cuts the debounce short so the table is current; the editor's
    # squiggles must catch up too.
    await user.open("/")
    calls = record_editor_calls(user)
    editor(user).set_value("name of operating system")
    await wait_for(lambda: published(calls))  # settled, so Breakdown is enabled
    calls.clear()
    editor(user).set_value("nme of operating sytem")
    await asyncio.sleep(0.05)  # click within the 300 ms debounce

    user.find(marker="breakdown").click()

    await wait_for(lambda: published(calls))
    [table] = user.find(marker="findings").elements
    assert "nme" in table.rows[0]["message"]
    assert published(calls)[-1]["text"] == "nme of operating sytem"


async def test_the_editor_lints_in_the_dialect_the_analysis_chose(user):
    await user.open("/")
    record_editor_calls(user)

    await set_text(user, "names of bes computers")
    await wait_for(lambda: editor(user).dialect == "session")

    await set_text(user, "name of operating system")
    await wait_for(lambda: editor(user).dialect == "client")


async def test_the_dialect_picker_reaches_the_editor_at_once(user):
    await user.open("/")
    calls = record_editor_calls(user)
    await set_text(user, "now")
    await wait_for(lambda: len(published(calls)) == 1)
    assert editor(user).dialect == "client"
    [picker] = user.find(marker="dialect").elements

    picker.set_value("session")

    await wait_for(lambda: len(published(calls)) == 2)
    assert editor(user).dialect == "session"


# --- fixes -----------------------------------------------------------------------

SETTING = 'exists values of setting "x" of client'
SETTING_FIXED = 'exists values of settings "x" of client'


async def test_fix_all_applies_the_analyzers_safe_fixes(user):
    await user.open("/")
    await set_text(user, SETTING)

    user.find(marker="fix-all").click()
    await asyncio.sleep(0.4)

    assert editor(user).value == SETTING_FIXED
    await user.should_not_see(marker="fix-all")


async def test_fix_all_is_absent_when_there_is_nothing_to_fix(user):
    await user.open("/")

    await set_text(user, "nme of operating system")

    await user.should_not_see(marker="fix-all")


async def test_fix_all_fixes_what_the_editor_shows_now(user):
    await user.open("/")
    await set_text(user, SETTING)

    # Typed inside the debounce: the button is from the old analysis.
    editor(user).set_value("  " + SETTING)
    user.find(marker="fix-all").click()

    assert editor(user).value == "  " + SETTING_FIXED


async def test_fix_all_refuses_when_the_text_no_longer_has_a_fix(user):
    await user.open("/")
    await set_text(user, SETTING)

    editor(user).set_value("name of operating system")
    user.find(marker="fix-all").click()

    await notified(user, "Nothing to fix")
    assert editor(user).value == "name of operating system"


def findings_table(user):
    [table] = user.find(marker="findings").elements
    return table


async def test_findings_offer_each_lead_as_a_fix(user):
    await user.open("/")

    await set_text(user, "nme of operating sytem")

    [row] = findings_table(user).rows
    fixes = {(f["name"], f["suggestion"]) for f in row["fixes"]}
    assert ("operating sytem", "operating system") in fixes
    assert any(name == "nme" for name, _ in fixes)


def test_suggestion_buttons_emit_what_the_handler_reads():
    # The page tests trigger the event directly, so pin the slot to the
    # same event name and payload keys the handler reads.
    slot = app_module._FINDINGS_SUGGESTIONS_SLOT

    assert f"$emit('{app_module.FIX_EVENT}', " in slot
    assert (
        f"{{{app_module.FIX_NAME}: fix.name, {app_module.FIX_SUGGESTION}: fix.suggestion}}" in slot
    )


def pick_lead(user, name, suggestion):
    user.find(marker="findings").trigger(
        app_module.FIX_EVENT,
        {app_module.FIX_NAME: name, app_module.FIX_SUGGESTION: suggestion},
    )


async def test_picking_a_lead_replaces_the_name(user):
    await user.open("/")
    await set_text(user, "name of operating sytem")

    pick_lead(user, "operating sytem", "operating system")

    assert editor(user).value == "name of operating system"


async def test_picking_a_lead_after_editing_it_away_warns(user):
    await user.open("/")
    await set_text(user, "name of operating sytem")

    editor(user).set_value("name of operating system")
    pick_lead(user, "operating sytem", "operating system")

    await notified(user, "no longer")
    assert editor(user).value == "name of operating system"


async def test_picking_a_lead_the_dialect_no_longer_offers_says_so(user):
    await user.open("/")
    [picker] = user.find(marker="dialect").elements
    picker.set_value("client")
    await set_text(user, 'names of filez of folder "/etc"')

    picker.set_value("session")  # session suggests other names for `filez`
    pick_lead(user, "filez", "file")

    await notified(user, "file no longer applies to filez")
    assert editor(user).value == 'names of filez of folder "/etc"'


async def test_evaluate_without_targets_warns(user):
    await user.open("/")

    user.find(marker="evaluate").click()

    await notified(user, "Pick at least one target")


async def test_evaluate_streams_result_rows(user, monkeypatch):
    seen: dict = {}

    async def fake_stream(text, targets, opts, **upstream) -> AsyncIterator[ClientRelevanceResult]:
        seen["targets"] = targets
        yield ClientRelevanceResult(
            host="local",
            transport="local",
            client_relevance=text,
            answers=["Linux Debian 11"],
            qna_version="11.0.7.61",
            elapsed_ms=42,
        )

    monkeypatch.setattr(app_module, "stream_results", fake_stream)
    await user.open("/")
    await set_text(user, "name of operating system")
    user.find(marker="target-local").click()

    user.find(marker="evaluate").click()

    await user.should_see("1 / 1")
    # Table cells are props, not child elements, so read the rows.
    [table] = user.find(marker="results").elements
    [row] = table.rows
    assert row["host"] == "local"
    assert row["preview"] == "Linux Debian 11"
    assert row["elapsed_ms"] == 42
    user.find(marker="results").trigger("update:expanded", [row["id"]])
    assert table.rows[0]["answers"] == "Linux Debian 11"
    assert [t.kind for t in seen["targets"]] == ["local"]


async def test_breakdown_shows_levels_and_first_failure(user, monkeypatch):
    async def fake_breakdown(view, target, opts, **upstream) -> list[LevelResult]:
        results = []
        for level in view.levels:
            failing = level.index == 1
            probe = ProbeView(
                level.probe,
                "not-evaluable" if failing else "count",
                None if failing else 3,
                1,
                1 if failing else 0,
            )
            results.append(LevelResult(level, probe, None, is_first_failure=failing))
        return results

    monkeypatch.setattr(app_module, "run_breakdown", fake_breakdown)
    await user.open("/")
    await set_text(user, 'names of files whose (size of it > 1000) of folder "/nope"')
    user.find(marker="target-local").click()

    user.find(marker="breakdown").click()

    await user.should_see("not-evaluable")
    await user.should_see(marker="first-failure")


async def test_inventory_error_is_shown_not_raised(no_inventory, monkeypatch, tmp_path):
    bad = tmp_path / "remote_clients.toml"
    bad.write_text("= nope", encoding="utf-8")
    monkeypatch.setattr(app_module, "find_inventory_path", lambda: bad)

    async with user_simulation(root=app_module.root) as user:
        await user.open("/")
        await user.should_see(marker="inventory-error")


async def test_inventory_targets_are_listed(no_inventory, monkeypatch, tmp_path):
    from bigfix_remote_client_relevance import Target

    monkeypatch.setattr(app_module, "find_inventory_path", lambda: tmp_path / "x.toml")
    monkeypatch.setattr(
        app_module,
        "load_inventory_targets",
        lambda path: InventoryLoad(
            path=path, targets=[Target(kind="ssh", name="lab-box")], error=None
        ),
    )

    async with user_simulation(root=app_module.root) as user:
        await user.open("/")
        await user.should_see(marker="inventory")


# --- shared inventory --------------------------------------------------------------

INVENTORY_TOML = '[hosts.shared-lab]\ntransport = "ssh"\nuser = "me"\n'


async def test_default_inventory_is_the_librarys_bigfix_remote_clients_toml(monkeypatch, tmp_path):
    # Same file, same search as `bigfix-remote-client-relevance`: nothing in
    # the current directory, so ~/.bigfix/remote_clients.toml is used.
    home = tmp_path / "home"
    (home / ".bigfix").mkdir(parents=True)
    (home / ".bigfix" / "remote_clients.toml").write_text(INVENTORY_TOML, encoding="utf-8")
    elsewhere = tmp_path / "cwd"
    elsewhere.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.delenv("SUDO_USER", raising=False)
    monkeypatch.chdir(elsewhere)
    monkeypatch.setattr(app_module, "_inventory_path", None)

    async with user_simulation(root=app_module.root) as user:
        await user.open("/")
        await user.should_see(marker="inventory")
        [row] = inventory_table(user).rows
        assert (row["label"], row["kind"]) == ("shared-lab", "ssh")
        title = hidden_element(user, "inventory-title").text
        assert str(home / ".bigfix" / "remote_clients.toml") in title


def test_inventory_flag_sets_path(captured_run, monkeypatch, tmp_path):
    monkeypatch.setattr(app_module, "_inventory_path", None)
    path = tmp_path / "custom.toml"

    main(["--inventory", str(path)])

    assert app_module._inventory_path == path


def test_no_inventory_flag_means_library_search(captured_run, monkeypatch):
    monkeypatch.setattr(app_module, "_inventory_path", None)

    main([])

    assert app_module._inventory_path is None


async def test_inventory_flag_overrides_search(monkeypatch, tmp_path):
    path = tmp_path / "custom.toml"
    path.write_text(INVENTORY_TOML, encoding="utf-8")
    monkeypatch.setattr(app_module, "_inventory_path", path)
    monkeypatch.setattr(app_module, "find_inventory_path", lambda: pytest.fail("searched"))

    async with user_simulation(root=app_module.root) as user:
        await user.open("/")
        await user.should_see(marker="inventory")


async def test_inventory_flag_missing_file_is_an_error_not_a_crash(monkeypatch, tmp_path):
    monkeypatch.setattr(app_module, "_inventory_path", tmp_path / "missing.toml")

    async with user_simulation(root=app_module.root) as user:
        await user.open("/")
        await user.should_see(marker="inventory-error")


async def test_breakdown_shows_probe_error_message_inline(user, monkeypatch):
    async def fake_breakdown(view, target, opts, **upstream) -> list[LevelResult]:
        message = "Singular expression refers to nonexistent object."
        return [
            LevelResult(
                level,
                ProbeView(level.probe, "error", None, 0, 0, error=message, error_kind="relevance"),
                None,
                is_first_failure=level.index == 1,
            )
            for level in view.levels
        ]

    monkeypatch.setattr(app_module, "run_breakdown", fake_breakdown)
    await user.open("/")
    await set_text(user, 'names of files whose (size of it > 1000) of folder "/nope"')
    user.find(marker="target-local").click()

    user.find(marker="breakdown").click()

    # Inline under the level, not only in the badge's hover tooltip.
    await user.should_see(marker="probe-error")
    texts = {e.text for e in user.find(marker="probe-error").elements}
    assert texts == {"relevance: Singular expression refers to nonexistent object."}


def button(user, marker):
    [element] = user.find(marker=marker).elements
    return element


def inventory_table(user):
    return hidden_element(user, "inventory")


def selected_ids(user) -> list[int]:
    return sorted(r["id"] for r in inventory_table(user).selected)


async def deselect(user, *ids: int) -> None:
    """What unticking hosts in the inventory table sends."""
    rows = [r for r in inventory_table(user).rows if r["id"] in ids]
    user.find(marker="inventory").trigger(
        "selection", {"rows": rows, "added": False, "keys": list(ids)}
    )
    await asyncio.sleep(0.05)


async def test_buttons_reset_after_a_run_finishes(user, monkeypatch):
    async def fake_stream(text, targets, opts, **upstream):
        yield ClientRelevanceResult(host="local", transport="local", client_relevance=text)

    monkeypatch.setattr(app_module, "stream_results", fake_stream)
    await user.open("/")
    await set_text(user, "name of operating system")
    user.find(marker="target-local").click()

    user.find(marker="evaluate").click()
    await user.should_see("1 / 1")
    await asyncio.sleep(0.1)

    assert button(user, "evaluate").enabled
    assert button(user, "breakdown").enabled
    assert not button(user, "cancel").enabled


async def test_cancel_stops_a_run_and_resets_buttons(user, monkeypatch):
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def slow_stream(text, targets, opts, **upstream):
        started.set()
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled.set()
            raise
        yield  # pragma: no cover - never reached

    monkeypatch.setattr(app_module, "stream_results", slow_stream)
    await user.open("/")
    await set_text(user, "name of operating system")
    user.find(marker="target-local").click()

    user.find(marker="evaluate").click()
    await asyncio.wait_for(started.wait(), 2)
    assert not button(user, "evaluate").enabled
    assert button(user, "cancel").enabled

    user.find(marker="cancel").click()
    await asyncio.wait_for(cancelled.wait(), 2)
    await asyncio.sleep(0.1)

    assert button(user, "evaluate").enabled
    assert not button(user, "cancel").enabled


# --- every inventory target selected by default ----------------------------------------

TWO_HOSTS = [Target(kind="ssh", name="lab-a"), Target(kind="ssh", name="lab-b")]


@pytest.fixture
async def inventory_user(monkeypatch, tmp_path):
    monkeypatch.setattr(app_module, "_inventory_path", tmp_path / "remote_clients.toml")
    monkeypatch.setattr(
        app_module,
        "load_inventory_targets",
        lambda path: InventoryLoad(path=path, targets=list(TWO_HOSTS), error=None),
    )
    async with user_simulation(root=app_module.root) as simulated:
        yield simulated


async def test_inventory_targets_are_all_selected_by_default(inventory_user):
    await inventory_user.open("/")

    await inventory_user.should_see(marker="inventory")
    assert selected_ids(inventory_user) == [0, 1]


async def test_evaluate_defaults_to_every_inventory_target(inventory_user, monkeypatch):
    seen: dict = {}

    async def fake_stream(text, targets, opts, **upstream):
        seen["targets"] = targets
        for target in targets:
            yield ClientRelevanceResult(
                host=target.label, transport="ssh", client_relevance=text, answers=["x"]
            )

    monkeypatch.setattr(app_module, "stream_results", fake_stream)
    await inventory_user.open("/")
    await set_text(inventory_user, "name of operating system")

    inventory_user.find(marker="evaluate").click()

    await inventory_user.should_see("2 / 2")
    assert seen["targets"] == TWO_HOSTS


async def test_breakdown_with_several_targets_picks_one_without_asking(inventory_user, monkeypatch):
    seen: dict = {}

    async def fake_breakdown(view, target, opts, **upstream):
        seen["target"] = target
        return []

    monkeypatch.setattr(app_module, "run_breakdown", fake_breakdown)
    await inventory_user.open("/")
    await set_text(inventory_user, "name of operating system")

    inventory_user.find(marker="breakdown").click()
    await asyncio.sleep(0.1)

    # Equal fit, same kind: selection order decides.
    assert seen["target"] == TWO_HOSTS[0]
    await inventory_user.should_not_see(marker="breakdown-target-choice")
    await inventory_user.should_see(marker="breakdown-reason")


async def test_breakdown_picks_the_target_matching_the_expressions_platform(monkeypatch, tmp_path):
    windows = Target(kind="ssh", name="win-box", platform="windows")
    inventory = [Target(kind="container", name="debian:11", image="debian:11"), windows]
    monkeypatch.setattr(app_module, "_inventory_path", tmp_path / "remote_clients.toml")
    monkeypatch.setattr(
        app_module,
        "load_inventory_targets",
        lambda path: InventoryLoad(path=path, targets=inventory, error=None),
    )
    seen: dict = {}

    async def fake_breakdown(view, target, opts, **upstream):
        seen["target"] = target
        return []

    monkeypatch.setattr(app_module, "run_breakdown", fake_breakdown)
    async with user_simulation(root=app_module.root) as user:
        await user.open("/")
        await set_text(user, 'exists key "HKLM\\Software" of registry')

        user.find(marker="breakdown").click()
        await asyncio.sleep(0.1)

    assert seen["target"] is windows


async def test_inventory_can_select_none(inventory_user):
    await inventory_user.open("/")
    await inventory_user.should_see(marker="inventory")

    await deselect(inventory_user, 0, 1)

    assert selected_ids(inventory_user) == []
    await count_reads(inventory_user, "0 systems")
    inventory_user.find(marker="evaluate").click()
    await notified(inventory_user, "Pick at least one target")


async def test_container_auto_arch_evaluates_every_published_arch(user, monkeypatch):
    seen: dict = {}

    async def fake_resolve(form, **kwargs):
        return {"debian:11": ["x86_64", "arm64"]}

    async def fake_stream(text, targets, opts, **upstream):
        seen["targets"] = targets
        for target in targets:
            yield ClientRelevanceResult(
                host=target.label, transport="container", client_relevance=text
            )

    monkeypatch.setattr(app_module, "resolve_container_arches", fake_resolve)
    monkeypatch.setattr(app_module, "stream_results", fake_stream)
    await user.open("/")
    await set_text(user, "name of operating system")
    user.find(marker="target-container").click()

    user.find(marker="evaluate").click()

    await user.should_see("2 / 2")
    assert [t.label for t in seen["targets"]] == [
        "container:debian:11@x86_64",
        "container:debian:11@arm64",
    ]


# --- first-run auto-discovery -------------------------------------------------------------


async def test_no_inventory_runs_discovery_and_selects_what_it_found(monkeypatch, tmp_path):
    written = tmp_path / ".bigfix" / "remote_clients.toml"
    monkeypatch.setattr(app_module, "_inventory_path", None)
    monkeypatch.setattr(app_module, "find_inventory_path", lambda: None)
    monkeypatch.setattr(app_module, "find_or_discover_inventory", lambda: written)
    monkeypatch.setattr(
        app_module,
        "load_inventory_targets",
        lambda path: (
            InventoryLoad(path=path, targets=list(TWO_HOSTS), error=None)
            if path == written
            else pytest.fail(f"loaded {path}")
        ),
    )

    async with user_simulation(root=app_module.root) as user:
        await user.open("/")
        await user.should_see(marker="inventory")
        assert selected_ids(user) == [0, 1]


async def test_discovery_progress_shows_while_it_runs(monkeypatch):
    release = threading.Event()

    def slow_discovery():
        release.wait(5)

    monkeypatch.setattr(app_module, "_inventory_path", None)
    monkeypatch.setattr(app_module, "find_inventory_path", lambda: None)
    monkeypatch.setattr(app_module, "find_or_discover_inventory", slow_discovery)

    async with user_simulation(root=app_module.root) as user:
        await user.open("/")
        try:
            await user.should_see(marker="inventory-discovering")
        finally:
            release.set()
        await user.should_not_see(marker="inventory-discovering", retries=20)


async def test_discovery_failure_is_shown_not_raised(monkeypatch):
    def broken():
        raise InventoryError("~/.bigfix is not writable")

    monkeypatch.setattr(app_module, "_inventory_path", None)
    monkeypatch.setattr(app_module, "find_inventory_path", lambda: None)
    monkeypatch.setattr(app_module, "find_or_discover_inventory", broken)

    async with user_simulation(root=app_module.root) as user:
        await user.open("/")
        await user.should_see(marker="inventory-error")


async def test_evaluate_waits_for_discovery_to_finish(monkeypatch, tmp_path):
    release = threading.Event()
    written = tmp_path / "remote_clients.toml"
    seen: dict = {}

    def slow_discovery():
        release.wait(5)
        return written

    async def fake_stream(text, targets, opts, **upstream):
        seen["targets"] = targets
        for target in targets:
            yield ClientRelevanceResult(host=target.label, transport="ssh", client_relevance=text)

    monkeypatch.setattr(app_module, "_inventory_path", None)
    monkeypatch.setattr(app_module, "find_inventory_path", lambda: None)
    monkeypatch.setattr(app_module, "find_or_discover_inventory", slow_discovery)
    monkeypatch.setattr(
        app_module,
        "load_inventory_targets",
        lambda path: InventoryLoad(path=path, targets=list(TWO_HOSTS), error=None),
    )
    monkeypatch.setattr(app_module, "stream_results", fake_stream)

    async with user_simulation(root=app_module.root) as user:
        await user.open("/")
        await set_text(user, "name of operating system")
        user.find(marker="evaluate").click()
        await asyncio.sleep(0.2)
        assert "targets" not in seen
        release.set()
        await user.should_see("2 / 2", retries=20)

    assert seen["targets"] == TWO_HOSTS


# --- review fixes ------------------------------------------------------------------------


async def slow_arches(form, **kwargs):
    await asyncio.sleep(0.2)  # stands in for the registry lookup
    return {}


async def test_double_click_starts_one_run(user, monkeypatch):
    runs: list[int] = []

    async def slow(text, targets, opts, **upstream):
        runs.append(1)
        await asyncio.sleep(1)
        yield  # pragma: no cover

    monkeypatch.setattr(app_module, "stream_results", slow)
    monkeypatch.setattr(app_module, "resolve_container_arches", slow_arches)
    await user.open("/")
    await set_text(user, "name of operating system")
    user.find(marker="target-local").click()

    user.find(marker="evaluate").click()
    assert not button(user, "evaluate").enabled
    assert not button(user, "breakdown").enabled
    assert button(user, "cancel").enabled
    await asyncio.sleep(0.05)
    user.find(marker="evaluate").click()
    await asyncio.sleep(0.5)

    assert len(runs) == 1


async def test_cancel_works_during_the_registry_lookup(user, monkeypatch):
    called: list[int] = []

    async def stream(text, targets, opts, **upstream):
        called.append(1)
        yield  # pragma: no cover

    monkeypatch.setattr(app_module, "stream_results", stream)
    monkeypatch.setattr(app_module, "resolve_container_arches", slow_arches)
    await user.open("/")
    await set_text(user, "name of operating system")
    user.find(marker="target-local").click()

    user.find(marker="evaluate").click()
    await asyncio.sleep(0.05)
    user.find(marker="cancel").click()
    await asyncio.sleep(0.4)

    assert called == []
    assert button(user, "evaluate").enabled


async def test_breakdown_uses_the_current_editor_text(user, monkeypatch):
    seen: list[str] = []

    async def fake_breakdown(analysis, target, opts, **upstream):
        seen.append(analysis.text)
        return []

    monkeypatch.setattr(app_module, "run_breakdown", fake_breakdown)
    await user.open("/")
    await set_text(user, "name of operating system")
    user.find(marker="target-local").click()

    editor(user).set_value('exists folder "/etc"')  # no sleep: inside the debounce
    user.find(marker="breakdown").click()
    await asyncio.sleep(0.1)

    assert seen == ['exists folder "/etc"']


async def test_breakdown_refuses_fresh_parse_error_inside_the_debounce(user, monkeypatch):
    ran: list[str] = []

    async def fake_breakdown(analysis, target, opts, **upstream):
        ran.append(analysis.text)
        return []

    monkeypatch.setattr(app_module, "run_breakdown", fake_breakdown)
    await user.open("/")
    await set_text(user, "name of operating system")
    user.find(marker="target-local").click()

    editor(user).set_value("name of (operating system")
    user.find(marker="breakdown").click()
    await asyncio.sleep(0.1)

    assert ran == []  # not the stale clean parse


async def test_evaluate_uses_the_current_editor_text(user, monkeypatch):
    seen: list[str] = []

    async def stream(text, targets, opts, **upstream):
        seen.append(text)
        yield ClientRelevanceResult(host="local", transport="local", client_relevance=text)

    monkeypatch.setattr(app_module, "stream_results", stream)
    await user.open("/")
    await set_text(user, "name of operating system")
    user.find(marker="target-local").click()

    editor(user).set_value("version of client")
    user.find(marker="evaluate").click()
    await asyncio.sleep(0.1)

    assert seen == ["version of client"]


async def test_enter_in_the_editor_evaluates(user, monkeypatch):
    seen: list[str] = []

    async def stream(text, targets, opts, **upstream):
        seen.append(text)
        yield ClientRelevanceResult(host="local", transport="local", client_relevance=text)

    monkeypatch.setattr(app_module, "stream_results", stream)
    await user.open("/")
    await set_text(user, "name of operating system")
    user.find(marker="target-local").click()

    editor(user).set_value("version of client")
    user.find(marker="editor").trigger("submit")
    await asyncio.sleep(0.1)

    assert seen == ["version of client"]


async def test_enter_while_running_starts_nothing(user, monkeypatch):
    release = asyncio.Event()
    seen: list[str] = []

    async def stream(text, targets, opts, **upstream):
        seen.append(text)
        await release.wait()
        yield ClientRelevanceResult(host="local", transport="local", client_relevance=text)

    monkeypatch.setattr(app_module, "stream_results", stream)
    await user.open("/")
    await set_text(user, "name of operating system")
    user.find(marker="target-local").click()

    user.find(marker="evaluate").click()
    await asyncio.sleep(0.1)
    user.find(marker="editor").trigger("submit")
    await asyncio.sleep(0.1)
    release.set()

    assert seen == ["name of operating system"]


async def test_first_run_discovery_does_not_hold_up_an_explicit_target(monkeypatch):
    release = threading.Event()
    called: list[int] = []

    def slow_discovery():
        release.wait(5)

    async def stream(text, targets, opts, **upstream):
        called.append(1)
        yield ClientRelevanceResult(host="local", transport="local", client_relevance=text)

    monkeypatch.setattr(app_module, "_inventory_path", None)
    monkeypatch.setattr(app_module, "find_inventory_path", lambda: None)
    monkeypatch.setattr(app_module, "find_or_discover_inventory", slow_discovery)
    monkeypatch.setattr(app_module, "stream_results", stream)

    async with user_simulation(root=app_module.root) as user:
        await user.open("/")
        try:
            await set_text(user, "name of operating system")
            user.find(marker="target-local").click()
            user.find(marker="evaluate").click()
            await asyncio.sleep(0.2)
            assert called == [1]
        finally:
            release.set()


@pytest.mark.parametrize(("field", "value"), [("timeout", 0), ("parallel", 0)])
async def test_zero_timeout_or_parallel_is_rejected_not_defaulted(user, monkeypatch, field, value):
    async def stream(text, targets, opts, **upstream):
        pytest.fail("should not run")
        yield  # pragma: no cover

    monkeypatch.setattr(app_module, "stream_results", stream)
    await user.open("/")
    await set_text(user, "name of operating system")
    user.find(marker="target-local").click()
    [number] = user.find(marker=f"option-{field}").elements
    number.set_value(value)

    user.find(marker="evaluate").click()

    await user.should_see("must be")


async def breakdown_table(user, monkeypatch, text):
    async def fake_breakdown(view, target, opts, **upstream):
        return [
            LevelResult(level, ProbeView(level.probe, "count", 1, 1, 0), None, False)
            for level in view.levels
        ]

    monkeypatch.setattr(app_module, "run_breakdown", fake_breakdown)
    await user.open("/")
    await set_text(user, text)
    user.find(marker="target-local").click()
    user.find(marker="breakdown").click()
    await user.should_see(marker="breakdown-level")


async def test_leaving_a_breakdown_cell_does_not_rebuild_the_findings(user, monkeypatch):
    await breakdown_table(user, monkeypatch, "name of operating sytem")
    [before] = user.find(marker="findings").elements
    cell = next(iter(user.find(marker="breakdown-level").elements))

    cell.props  # noqa: B018 - element exists
    user.find(marker="breakdown-level").trigger("mouseenter")
    user.find(marker="breakdown-level").trigger("mouseleave")

    [after] = user.find(marker="findings").elements
    assert after is before


async def test_hover_highlights_the_level_span(user, monkeypatch):
    await breakdown_table(user, monkeypatch, WHOSE)

    user.find(marker="breakdown-level").trigger("mouseenter")

    assert any(d.get("class") == "bfrd-level" for d in editor(user).decorations)


async def test_hover_after_editing_the_text_highlights_nothing_and_dims_the_table(
    user, monkeypatch
):
    await breakdown_table(user, monkeypatch, WHOSE)

    await set_text(user, WHOSE.replace("/etc", "/var"))
    user.find(marker="breakdown-level").trigger("mouseenter")

    assert not any(d.get("class") == "bfrd-level" for d in editor(user).decorations)
    await user.should_see(marker="breakdown-stale")


WHOSE = 'names of files whose (size of it > 1000) of folder "/etc"'


async def test_closing_the_tab_after_a_double_click_cleans_up_the_one_run(user, monkeypatch):
    started: list[int] = []
    cleaned: list[int] = []

    async def slow(text, targets, opts, **upstream):
        started.append(1)
        try:
            await asyncio.sleep(5)
            yield  # pragma: no cover
        finally:
            cleaned.append(1)

    monkeypatch.setattr(app_module, "stream_results", slow)
    monkeypatch.setattr(app_module, "resolve_container_arches", slow_arches)
    await user.open("/")
    await set_text(user, "name of operating system")
    user.find(marker="target-local").click()
    user.find(marker="evaluate").click()
    await asyncio.sleep(0.05)
    user.find(marker="evaluate").click()
    await asyncio.sleep(0.4)

    [button_element] = user.find(marker="evaluate").elements
    button_element.client.delete()
    await asyncio.sleep(0.2)

    assert started == [1]
    assert cleaned == [1]


# --- results area (issue 4) ----------------------------------------------------------------


def one_result_stream(answers=("Linux Debian 11",)):
    async def stream(text, targets, opts, **upstream):
        yield ClientRelevanceResult(
            host="local", transport="local", client_relevance=text, answers=list(answers)
        )

    return stream


def counting_breakdown():
    async def fake_breakdown(view, target, opts, **upstream):
        return [
            LevelResult(level, ProbeView(level.probe, "count", 1, 1, 0), None, False)
            for level in view.levels
        ]

    return fake_breakdown


async def evaluate_and_break_down(user, monkeypatch, *, breakdown_first: bool) -> None:
    monkeypatch.setattr(app_module, "stream_results", one_result_stream())
    monkeypatch.setattr(app_module, "run_breakdown", counting_breakdown())
    await user.open("/")
    await set_text(user, WHOSE)
    user.find(marker="target-local").click()

    async def run_evaluate():
        user.find(marker="evaluate").click()
        await user.should_see("1 / 1")
        await asyncio.sleep(0.1)

    async def run_breakdown_():
        user.find(marker="breakdown").click()
        await user.should_see(marker="breakdown-level")
        await asyncio.sleep(0.1)

    for step in (
        (run_breakdown_, run_evaluate) if breakdown_first else (run_evaluate, run_breakdown_)
    ):
        await step()


def section_order(user, *markers):
    elements = [button(user, m) for m in markers]
    siblings = elements[0].parent_slot.children
    return [siblings.index(e) for e in elements]


async def test_breakdown_renders_above_results(user, monkeypatch):
    await evaluate_and_break_down(user, monkeypatch, breakdown_first=False)

    breakdown_at, results_at = section_order(user, "breakdown-section", "results-section")
    assert breakdown_at < results_at


async def test_evaluate_after_breakdown_keeps_the_breakdown(user, monkeypatch):
    await evaluate_and_break_down(user, monkeypatch, breakdown_first=True)

    await user.should_see(marker="breakdown-level")


async def test_breakdown_after_evaluate_keeps_the_results(user, monkeypatch):
    await evaluate_and_break_down(user, monkeypatch, breakdown_first=False)

    assert result_hosts(user) == ["local"]


def result_hosts(user) -> list[str]:
    [table] = user.find(marker="results").elements
    return [row["host"] for row in table.rows]


def result(host, answers=(), **kwargs):
    # container: its display host is the host as given (ssh would read "ssh:<host>").
    return ClientRelevanceResult(
        host=host, transport="container", client_relevance="x", answers=list(answers), **kwargs
    )


def stream_of(*results, gate: asyncio.Event | None = None, after: int = 0):
    """Yield ``results``; with ``gate``, pause after the first ``after`` until it's set."""

    async def stream(text, targets, opts, **upstream):
        for i, r in enumerate(results):
            if gate is not None and i == after:
                await gate.wait()
            yield r

    stream.count = len(results)  # type: ignore[attr-defined]
    return stream


async def evaluate_with(user, monkeypatch, stream) -> None:
    monkeypatch.setattr(app_module, "stream_results", stream)
    # One Local target, but the fake yields several results: keep "N / M" honest.
    monkeypatch.setattr(app_module, "total_work", lambda targets, opts: stream.count)
    await user.open("/")
    await set_text(user, "name of operating system")
    user.find(marker="target-local").click()
    user.find(marker="evaluate").click()


def results_table(user):
    [table] = user.find(marker="results").elements
    return table


MIXED = (
    result("empty"),
    result("err", error="boom", error_kind="transport"),
    result("three", ["a", "b", "c"]),
)


async def test_results_are_one_table_sorted_by_answer_count(user, monkeypatch):
    await evaluate_with(user, monkeypatch, stream_of(*MIXED))
    await user.should_see("3 / 3")

    assert result_hosts(user) == ["three", "empty", "err"]
    assert [r["ok"] for r in results_table(user).rows] == [True, True, False]


async def test_results_resort_as_they_stream(user, monkeypatch):
    gate = asyncio.Event()
    await evaluate_with(
        user,
        monkeypatch,
        stream_of(result("one", ["a"]), result("two", ["a", "b"]), gate=gate, after=1),
    )
    await user.should_see("1 / 2")
    assert result_hosts(user) == ["one"]

    gate.set()
    await user.should_see("2 / 2")

    assert result_hosts(user) == ["two", "one"]


async def test_expanded_rows_stay_expanded_when_more_results_arrive(user, monkeypatch):
    gate = asyncio.Event()
    await evaluate_with(
        user,
        monkeypatch,
        stream_of(result("one", ["a"]), result("two", ["a", "b"]), gate=gate, after=1),
    )
    await user.should_see("1 / 2")
    [first] = results_table(user).rows

    user.find(marker="results").trigger("update:expanded", [first["id"]])
    gate.set()
    await user.should_see("2 / 2")

    assert results_table(user).props["expanded"] == [first["id"]]


async def test_summary_line_counts_results(user, monkeypatch):
    await evaluate_with(user, monkeypatch, stream_of(*MIXED))

    await user.should_see("3 results: 1 with answers, 1 empty, 1 error")


async def test_results_section_hidden_before_any_run(user):
    await user.open("/")

    await user.should_not_see(marker="results")
    await user.should_not_see(marker="results-summary")


async def test_answer_text_is_not_rendered_as_html(user, monkeypatch):
    # The body slot must interpolate ({{ }}), never v-html: answers come from the endpoint.
    await evaluate_with(user, monkeypatch, stream_of(result("h", ["<b>x</b>"])))
    await user.should_see("1 / 1")

    slots = "".join(str(s.template) for s in results_table(user).slots.values() if s.template)
    assert "v-html" not in slots
    assert results_table(user).rows[0]["preview"] == "<b>x</b>"


# --- search (issue 4) ----------------------------------------------------------------------

# A host that lists two qna versions yields two results with the same host.
FLEET = (
    result("deb", ["Linux Debian 11"], qna_version="a"),
    result("rh", ["Linux Red Hat 9"], qna_version="a"),
    result("multi", ["Linux Debian 12"], qna_version="a"),
    result("multi", [], qna_version="b"),
    result("down", error="Permission denied (publickey)", error_kind="transport"),
)


async def evaluated_fleet(user, monkeypatch):
    await evaluate_with(user, monkeypatch, stream_of(*FLEET))
    await user.should_see(f"{len(FLEET)} / {len(FLEET)}")
    await asyncio.sleep(0.1)


async def search(user, text):
    user.find(marker="results-filter").clear().type(text)
    await asyncio.sleep(app_module.FILTER_DEBOUNCE_S + 0.1)


async def clear_search(user):
    user.find(marker="results-filter").clear()
    await asyncio.sleep(app_module.FILTER_DEBOUNCE_S + 0.1)


async def test_search_hides_hosts_without_a_matching_result(user, monkeypatch):
    await evaluated_fleet(user, monkeypatch)

    await search(user, "debian")

    assert result_hosts(user) == ["deb", "multi", "multi"]


async def test_search_shows_every_result_of_a_matching_host(user, monkeypatch):
    await evaluated_fleet(user, monkeypatch)

    await search(user, "DEBIAN 12")

    rows = results_table(user).rows
    assert [(r["host"], r["qna"]) for r in rows] == [("multi", "a"), ("multi", "b")]


async def test_search_expands_matching_hosts(user, monkeypatch):
    await evaluated_fleet(user, monkeypatch)

    await search(user, "debian")

    table = results_table(user)
    assert table.props["expanded"] == sorted(r["id"] for r in table.rows)


async def test_clearing_search_restores_all_rows_and_what_was_expanded(user, monkeypatch):
    await evaluated_fleet(user, monkeypatch)
    rh = next(r["id"] for r in results_table(user).rows if r["host"] == "rh")
    user.find(marker="results").trigger("update:expanded", [rh])

    await search(user, "debian")
    await clear_search(user)

    assert len(results_table(user).rows) == len(FLEET)
    assert results_table(user).props["expanded"] == [rh]


async def test_search_matches_error_text(user, monkeypatch):
    await evaluated_fleet(user, monkeypatch)

    await search(user, "publickey")

    assert result_hosts(user) == ["down"]


async def test_search_summary_says_showing_n_of_m(user, monkeypatch):
    await evaluated_fleet(user, monkeypatch)

    await search(user, "debian")

    await user.should_see(
        'Showing 3 of 5 results: hosts with an answer or error containing "debian"'
    )


async def test_search_with_no_matches_empties_the_table_and_says_so(user, monkeypatch):
    await evaluated_fleet(user, monkeypatch)

    await search(user, "solaris")

    assert results_table(user).rows == []
    await user.should_see("Showing 0 of 5 results")


async def test_search_persists_across_evaluate(user, monkeypatch):
    await evaluated_fleet(user, monkeypatch)
    await search(user, "red hat")

    user.find(marker="evaluate").click()
    await asyncio.sleep(0.3)

    assert result_hosts(user) == ["rh"]
    assert button(user, "results-filter").value == "red hat"


async def test_new_matches_streaming_in_are_expanded(user, monkeypatch):
    gate = asyncio.Event()
    await evaluate_with(
        user,
        monkeypatch,
        stream_of(result("a", ["debian"]), result("b", ["debian too"]), gate=gate, after=1),
    )
    await user.should_see("1 / 2")
    await search(user, "debian")

    gate.set()
    await user.should_see("2 / 2")

    table = results_table(user)
    assert table.props["expanded"] == sorted(r["id"] for r in table.rows)
    assert len(table.rows) == 2


async def test_export_includes_results_hidden_by_the_search(user, monkeypatch):
    exported: list[str] = []
    # The simulated user's download treats a str payload as a URL; capture it instead.
    monkeypatch.setattr(
        UserDownload, "content", lambda self, payload, *a, **k: exported.append(payload)
    )
    await evaluated_fleet(user, monkeypatch)
    await search(user, "debian")

    user.find(marker="export").click()

    [payload] = exported
    assert len(json.loads(payload)) == len(FLEET)


# --- clear (issue 4) -----------------------------------------------------------------------


def hidden_element(user, marker):
    [element] = [e for e in user.client.elements.values() if marker in e._markers]
    return element


async def test_clear_is_disabled_until_there_is_something_to_clear(user):
    await user.open("/")

    assert not button(user, "clear").enabled


async def test_clear_removes_results_breakdown_progress_and_search(user, monkeypatch):
    await evaluate_and_break_down(user, monkeypatch, breakdown_first=False)
    await search(user, "debian")
    assert button(user, "clear").enabled

    user.find(marker="clear").click()

    await user.should_not_see(marker="results")
    await user.should_not_see(marker="breakdown-level")
    await user.should_not_see("1 / 1")
    # The results section is hidden now, and user.find only sees what's visible.
    assert hidden_element(user, "results-filter").value == ""
    assert hidden_element(user, "results").rows == []
    assert hidden_element(user, "results").props["expanded"] == []
    assert not button(user, "export").enabled
    assert not button(user, "clear").enabled


async def test_clear_after_breakdown_only_is_enabled_and_clears_it(user, monkeypatch):
    await breakdown_table(user, monkeypatch, WHOSE)
    await asyncio.sleep(0.1)
    assert button(user, "clear").enabled

    user.find(marker="clear").click()

    await user.should_not_see(marker="breakdown-level")


async def test_clear_is_disabled_while_running(user, monkeypatch):
    gate = asyncio.Event()
    await evaluate_with(
        user, monkeypatch, stream_of(result("a", ["1"]), result("b"), gate=gate, after=1)
    )
    await user.should_see("1 / 2")

    assert not button(user, "clear").enabled

    user.find(marker="cancel").click()
    await asyncio.sleep(0.2)

    assert button(user, "clear").enabled


async def test_clear_keeps_the_activity_log(user, monkeypatch):
    gate = asyncio.Event()
    await evaluate_with(
        user, monkeypatch, stream_of(result("a", ["1"]), result("b"), gate=gate, after=1)
    )
    await user.should_see("1 / 2")
    user.find(marker="cancel").click()
    await user.should_see("cancelled")

    user.find(marker="clear").click()

    await user.should_see("cancelled")


async def test_breakdown_after_clear_is_not_stale(user, monkeypatch):
    await breakdown_table(user, monkeypatch, WHOSE)
    await asyncio.sleep(0.1)
    user.find(marker="clear").click()

    user.find(marker="breakdown").click()
    await user.should_see(marker="breakdown-level")

    await user.should_not_see(marker="breakdown-stale")


async def test_results_table_shows_the_qna_version_so_a_hosts_rows_differ(user, monkeypatch):
    # A host listing two qna versions gives two rows with the same Target.
    await evaluated_fleet(user, monkeypatch)

    table = results_table(user)
    assert "qna" in [c["name"] for c in table.columns]
    multi = [r["qna"] for r in table.rows if r["host"] == "multi"]
    assert multi == ["a", "b"]


# --- PR #5 review ---------------------------------------------------------------------------


async def test_rows_the_user_collapses_stay_collapsed_while_filtered_results_stream_in(
    user, monkeypatch
):
    gate = asyncio.Event()
    await evaluate_with(
        user,
        monkeypatch,
        stream_of(result("a", ["debian"]), result("b", ["debian too"]), gate=gate, after=1),
    )
    await user.should_see("1 / 2")
    await search(user, "debian")
    [a] = results_table(user).rows
    assert results_table(user).props["expanded"] == [a["id"]]

    user.find(marker="results").trigger("update:expanded", [])  # the user collapses it
    gate.set()
    await user.should_see("2 / 2")

    table = results_table(user)
    b = next(r["id"] for r in table.rows if r["host"] == "b")
    assert table.props["expanded"] == [b]  # the new match opens; "a" stays shut


async def test_each_result_is_converted_to_a_table_row_once(user, monkeypatch):
    calls: list[int] = []
    real = app_module.results_table_row

    def counting(key, row):
        calls.append(key)
        return real(key, row)

    monkeypatch.setattr(app_module, "results_table_row", counting)
    await evaluated_fleet(user, monkeypatch)
    await search(user, "debian")
    await clear_search(user)

    assert sorted(calls) == list(range(len(FLEET)))


async def test_only_expanded_rows_carry_their_details(user, monkeypatch):
    await evaluated_fleet(user, monkeypatch)
    table = results_table(user)
    assert all("raw" not in r and "answers" not in r for r in table.rows)

    deb = next(r["id"] for r in table.rows if r["host"] == "deb")
    user.find(marker="results").trigger("update:expanded", [deb])

    [opened] = [r for r in table.rows if "answers" in r]
    assert opened["id"] == deb
    assert opened["answers"] == "Linux Debian 11"


async def test_the_filter_waits_for_typing_to_pause(user, monkeypatch):
    await evaluated_fleet(user, monkeypatch)

    user.find(marker="results-filter").type("debian")
    assert len(results_table(user).rows) == len(FLEET)  # not yet

    await asyncio.sleep(app_module.FILTER_DEBOUNCE_S + 0.1)
    assert result_hosts(user) == ["deb", "multi", "multi"]


# --- session relevance (issue 7) ---------------------------------------------------------

SERVER = "bigfix.example.com"


def fake_session(calls: list[str], **fields):
    """An evaluate_session stand-in answering every query the same way."""
    fields = {"answers": ("35",), "answer_types": ("integer",), "plural": False} | fields

    async def evaluate_session(text, opts, **upstream):
        calls.append(text)
        return SessionEvaluation(text, SERVER, elapsed_ms=5, **fields)

    return evaluate_session


def dialect_reason(user) -> str:
    [label] = user.find(marker="dialect-reason").elements
    return label.text


async def test_dialect_picker_defaults_to_auto_and_says_why(user):
    await user.open("/")

    await set_text(user, "names of bes computers")

    [picker] = user.find(marker="dialect").elements
    assert picker.value == "auto"
    assert "bes computers" in dialect_reason(user)
    await user.should_see("dialect: session")


async def test_session_only_evaluate_needs_no_client_targets(user, monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(app_module, "evaluate_session", fake_session(calls))

    async def no_stream(*args, **kwargs):
        raise AssertionError("client relevance must not run")
        yield  # pragma: no cover

    monkeypatch.setattr(app_module, "stream_results", no_stream)
    await user.open("/")
    await set_text(user, "number of bes computers")

    user.find(marker="evaluate").click()

    await user.should_see("1 / 1")
    assert calls == ["number of bes computers"]
    [row] = results_table(user).rows
    assert row["host"] == SERVER
    assert row["dialect"] == "session"
    assert row["preview"] == "35"


async def test_both_dialects_list_the_session_result_first(user, monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(app_module, "evaluate_session", fake_session(calls))
    monkeypatch.setattr(app_module, "stream_results", stream_of(result("local", ["a", "b"])))
    await user.open("/")
    await set_text(user, "now")
    user.find(marker="target-local").click()

    user.find(marker="evaluate").click()

    await user.should_see("2 / 2")
    assert calls == ["now"]
    # The client row has more answers, but session results always list first.
    assert [r["dialect"] for r in results_table(user).rows] == ["session", "client"]


async def test_dialect_override_runs_only_what_was_picked(user, monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(app_module, "evaluate_session", fake_session(calls))
    monkeypatch.setattr(app_module, "stream_results", stream_of(result("local", ["a"])))
    await user.open("/")
    await set_text(user, "now")
    user.find(marker="target-local").click()
    [picker] = user.find(marker="dialect").elements

    picker.set_value("client")

    assert "set to client" in dialect_reason(user)
    user.find(marker="evaluate").click()
    await user.should_see("1 / 1")
    assert calls == []
    assert [r["dialect"] for r in results_table(user).rows] == ["client"]


async def test_both_without_client_targets_still_runs_session_and_says_so(user, monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(app_module, "evaluate_session", fake_session(calls))
    await user.open("/")
    await set_text(user, "now")

    user.find(marker="evaluate").click()

    await user.should_see("1 / 1")
    await user.should_see("session relevance only")
    assert calls == ["now"]


async def test_both_with_a_bad_target_field_warns_and_runs_nothing(user, monkeypatch):
    calls: list[str] = []
    streamed: list[list[Target]] = []
    monkeypatch.setattr(app_module, "evaluate_session", fake_session(calls))

    async def fake_stream(text, targets, opts, **upstream):
        streamed.append(targets)
        for _ in ():
            yield

    monkeypatch.setattr(app_module, "stream_results", fake_stream)
    await user.open("/")
    await set_text(user, "now")
    user.find(marker="target-local").click()
    # Ticked with the host left blank: a mistake to fix, not "no client targets".
    user.find(marker="target-ssh").click()

    user.find(marker="evaluate").click()

    await notified(user, "SSH target needs a host")
    await asyncio.sleep(0.1)
    assert streamed == []
    assert calls == []


async def test_session_error_shows_as_an_error_row(user, monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(
        app_module,
        "evaluate_session",
        fake_session(
            calls, answers=(), error="No BigFix server is configured", error_kind="config"
        ),
    )
    await user.open("/")
    await set_text(user, "names of bes computers")

    user.find(marker="evaluate").click()

    await user.should_see("1 / 1")
    [row] = results_table(user).rows
    assert row["ok"] is False
    assert row["status"] == "config"
    assert "No BigFix server" in row["preview"]


NO_SERVER = {"answers": (), "error": "No BigFix server is configured", "error_kind": "config"}


async def test_no_server_hides_the_session_row_auto_added(user, monkeypatch):
    # `now` runs both only because it's valid in either; most users have no
    # root server, and a red row on every such run would be noise.
    calls: list[str] = []
    monkeypatch.setattr(app_module, "evaluate_session", fake_session(calls, **NO_SERVER))
    monkeypatch.setattr(app_module, "stream_results", stream_of(result("local", ["a"])))
    await user.open("/")
    await set_text(user, "now")
    user.find(marker="target-local").click()

    user.find(marker="evaluate").click()

    await user.should_see("2 / 2")
    await user.should_see("session relevance skipped")
    assert calls == ["now"]
    assert [r["dialect"] for r in results_table(user).rows] == ["client"]


async def test_no_server_still_shows_when_both_was_picked(user, monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(app_module, "evaluate_session", fake_session(calls, **NO_SERVER))
    monkeypatch.setattr(app_module, "stream_results", stream_of(result("local", ["a"])))
    await user.open("/")
    await set_text(user, "now")
    user.find(marker="target-local").click()
    [picker] = user.find(marker="dialect").elements
    picker.set_value("both")

    user.find(marker="evaluate").click()

    await user.should_see("2 / 2")
    rows = results_table(user).rows
    assert [(r["dialect"], r["status"]) for r in rows] == [("session", "config"), ("client", "ok")]


async def test_no_server_still_shows_when_session_is_all_that_runs(user, monkeypatch):
    # Auto picked both, but with no client targets the session row is the
    # whole result, so hiding it would leave nothing to say what happened.
    calls: list[str] = []
    monkeypatch.setattr(app_module, "evaluate_session", fake_session(calls, **NO_SERVER))
    await user.open("/")
    await set_text(user, "now")

    user.find(marker="evaluate").click()

    await user.should_see("1 / 1")
    [row] = results_table(user).rows
    assert row["status"] == "config"


async def test_session_only_expression_cannot_break_down(user):
    await user.open("/")

    await set_text(user, "names of bes computers")

    [button] = user.find(marker="breakdown").elements
    assert not button.enabled
    await user.should_see("session relevance can't break down yet")


async def test_export_lists_session_results_first(user, monkeypatch):
    exported: list[str] = []
    monkeypatch.setattr(
        UserDownload, "content", lambda self, payload, *a, **k: exported.append(payload)
    )
    monkeypatch.setattr(app_module, "evaluate_session", fake_session([]))
    monkeypatch.setattr(app_module, "stream_results", stream_of(result("local", ["a"])))
    await user.open("/")
    await set_text(user, "now")
    user.find(marker="target-local").click()
    user.find(marker="evaluate").click()
    await user.should_see("2 / 2")

    user.find(marker="export").click()

    [payload] = exported
    entries = json.loads(payload)
    assert entries[0]["dialect"] == "session"
    assert entries[0]["host"] == SERVER
    assert entries[1]["host"] == "local"


async def test_clear_removes_session_results_too(user, monkeypatch):
    monkeypatch.setattr(app_module, "evaluate_session", fake_session([]))
    await user.open("/")
    await set_text(user, "number of bes computers")
    user.find(marker="evaluate").click()
    await user.should_see("1 / 1")

    user.find(marker="clear").click()

    # Clear hides the results section, so look the table up among all elements.
    assert hidden_element(user, "results").rows == []
    [export] = user.find(marker="export").elements
    assert not export.enabled


# --- answer summary (issue 8) ---------------------------------------------------------------

# Two systems agree, one differs, one answers nothing, one fails.
SUMMARY_FLEET = (
    result("deb", ["Linux Debian 11"]),
    result("deb2", ["Linux Debian 11"]),
    result("rh", ["Linux Red Hat 9"]),
    result("empty"),
    result("down", error="Permission denied (publickey)", error_kind="transport"),
)


def answers_table(user):
    # Looked up among all elements: the summary is hidden for fewer than 2 systems.
    return hidden_element(user, "answers")


def answer_rows(user) -> list[tuple[str, int]]:
    return [(r["label"], r["systems"]) for r in answers_table(user).rows]


async def evaluated_summary_fleet(user, monkeypatch, *results):
    results = results or SUMMARY_FLEET
    await evaluate_with(user, monkeypatch, stream_of(*results))
    await user.should_see(f"{len(results)} / {len(results)}")
    await asyncio.sleep(0.1)


async def test_sections_go_breakdown_then_answers_then_results(user, monkeypatch):
    monkeypatch.setattr(app_module, "run_breakdown", counting_breakdown())
    await evaluated_summary_fleet(user, monkeypatch)
    user.find(marker="breakdown").click()
    await user.should_see(marker="breakdown-level")

    breakdown_at, answers_at, results_at = section_order(
        user, "breakdown-section", "answers-section", "results-section"
    )
    assert breakdown_at < answers_at < results_at


async def test_answer_summary_counts_systems_per_answer_highest_first(user, monkeypatch):
    await evaluated_summary_fleet(user, monkeypatch)

    await user.should_see(marker="answers")
    assert answer_rows(user) == [
        ("Linux Debian 11", 2),
        ("Linux Red Hat 9", 1),
        ("(no answers)", 1),
        ("error: transport", 1),
    ]
    assert [r["percent"] for r in answers_table(user).rows] == [40, 20, 20, 20]


async def test_answer_summary_title_counts_systems(user, monkeypatch):
    await evaluated_summary_fleet(user, monkeypatch)

    [title] = user.find(marker="answers-title").elements
    assert "5 systems" in title.text


async def test_answer_summary_hidden_for_a_single_system(user, monkeypatch):
    await evaluated_summary_fleet(user, monkeypatch, result("solo", ["a", "b", "c"]))

    await user.should_see(marker="results")
    await user.should_not_see(marker="answers-section")
    assert not hidden_element(user, "answers-section").visible


async def test_answer_summary_hidden_before_any_run(user):
    await user.open("/")

    await user.should_not_see(marker="answers-section")
    assert not hidden_element(user, "answers-section").visible


async def test_answer_summary_updates_as_results_stream(user, monkeypatch):
    gate = asyncio.Event()
    stream = stream_of(
        result("a", ["x"]), result("b", ["x"]), result("c", ["x"]), gate=gate, after=2
    )
    await evaluate_with(user, monkeypatch, stream)
    await user.should_see("2 / 3")
    assert answer_rows(user) == [("x", 2)]

    gate.set()

    await user.should_see("3 / 3")
    assert answer_rows(user) == [("x", 3)]


async def test_answer_summary_ignores_the_filter(user, monkeypatch):
    await evaluated_summary_fleet(user, monkeypatch)
    before = answer_rows(user)

    await search(user, "debian")

    assert result_hosts(user) == ["deb", "deb2"]
    assert answer_rows(user) == before


async def test_answer_summary_says_how_many_more(user, monkeypatch):
    many = [f"file{i:02}" for i in range(60)]
    await evaluated_summary_fleet(user, monkeypatch, result("a", many), result("b", many[:5]))

    assert len(answer_rows(user)) == app_module.TALLY_LIMIT
    [more] = user.find(marker="answers-more").elements
    assert more.text.startswith("+10 more distinct answers")


async def test_answer_summary_more_caption_hidden_when_everything_fits(user, monkeypatch):
    await evaluated_summary_fleet(user, monkeypatch)

    await user.should_not_see(marker="answers-more")
    assert not hidden_element(user, "answers-more").visible


async def test_answer_summary_is_not_rendered_as_html(user, monkeypatch):
    await evaluated_summary_fleet(user, monkeypatch, result("a", ["<b>x</b>"]), result("b"))

    slots = "".join(str(s.template) for s in answers_table(user).slots.values() if s.template)
    assert "v-html" not in slots
    assert answer_rows(user)[0] == ("<b>x</b>", 1)


async def test_clear_removes_the_answer_summary(user, monkeypatch):
    await evaluated_summary_fleet(user, monkeypatch)

    user.find(marker="clear").click()

    await user.should_not_see(marker="answers-section")
    assert answers_table(user).rows == []


async def test_answer_summary_ignores_session_results(user, monkeypatch):
    monkeypatch.setattr(app_module, "evaluate_session", fake_session([]))
    monkeypatch.setattr(
        app_module, "stream_results", stream_of(result("a", ["x"]), result("b", ["y"]))
    )
    monkeypatch.setattr(app_module, "total_work", lambda targets, opts: 2)
    await user.open("/")
    await set_text(user, "now")
    user.find(marker="target-local").click()

    user.find(marker="evaluate").click()

    await user.should_see("3 / 3")
    assert answer_rows(user) == [("x", 1), ("y", 1)]
    [title] = user.find(marker="answers-title").elements
    assert "2 systems" in title.text


async def test_each_result_is_tallied_once(user, monkeypatch):
    calls: list[str] = []
    real = AnswerTally.add

    def counting(self, row):
        calls.append(row.host)
        real(self, row)

    monkeypatch.setattr(AnswerTally, "add", counting)
    await evaluated_summary_fleet(user, monkeypatch)
    await search(user, "debian")
    await clear_search(user)

    assert sorted(calls) == sorted(r.host for r in SUMMARY_FLEET)


# --- drill-down from the answer summary (issue 8) --------------------------------------------

# "multi" lists two qna versions: one answers like deb, the other answers nothing.
PICK_FLEET = (
    result("deb", ["Linux Debian 11"], qna_version="a"),
    result("rh", ["Linux Red Hat 9"], qna_version="a"),
    result("multi", ["Linux Debian 11"], qna_version="a"),
    result("multi", [], qna_version="b"),
    result("down", error="Permission denied (publickey)", error_kind="transport"),
)


async def pick(user, label):
    [row] = [r for r in answers_table(user).rows if r["label"] == label]
    user.find(marker="answers").trigger("pick", row["key"])
    await asyncio.sleep(0.1)


def pick_chip(user):
    return hidden_element(user, "results-pick")


def summary_text(user) -> str:
    [label] = user.find(marker="results-summary").elements
    return label.text


async def test_clicking_an_answer_shows_only_systems_with_it(user, monkeypatch):
    await evaluated_summary_fleet(user, monkeypatch, *PICK_FLEET)

    await pick(user, "Linux Debian 11")

    # multi keeps both its rows, the one without the answer too.
    assert result_hosts(user) == ["deb", "multi", "multi"]
    await user.should_see(marker="results-pick")
    assert "Linux Debian 11" in pick_chip(user).text


async def test_picked_summary_row_is_marked(user, monkeypatch):
    await evaluated_summary_fleet(user, monkeypatch, *PICK_FLEET)

    await pick(user, "Linux Debian 11")

    assert [r["label"] for r in answers_table(user).rows if r["picked"]] == ["Linux Debian 11"]


async def test_clicking_the_picked_answer_again_removes_the_pick(user, monkeypatch):
    await evaluated_summary_fleet(user, monkeypatch, *PICK_FLEET)
    await pick(user, "Linux Debian 11")
    await user.should_see(marker="results-pick")

    await pick(user, "Linux Debian 11")

    assert sorted(set(result_hosts(user))) == ["deb", "down", "multi", "rh"]
    await user.should_not_see(marker="results-pick")
    assert not any(r["picked"] for r in answers_table(user).rows)


async def test_removing_the_pick_chip_shows_everything(user, monkeypatch):
    await evaluated_summary_fleet(user, monkeypatch, *PICK_FLEET)
    await pick(user, "Linux Debian 11")

    pick_chip(user).set_value(False)  # what its ✕ does
    await asyncio.sleep(0.1)

    assert len(result_hosts(user)) == len(PICK_FLEET)
    await user.should_not_see(marker="results-pick")


async def test_a_new_pick_after_removing_the_chip_shows_the_chip_again(user, monkeypatch):
    await evaluated_summary_fleet(user, monkeypatch, *PICK_FLEET)
    await pick(user, "Linux Debian 11")
    pick_chip(user).set_value(False)
    await asyncio.sleep(0.1)

    await pick(user, "Linux Red Hat 9")

    await user.should_see(marker="results-pick")
    assert result_hosts(user) == ["rh"]


async def test_pick_no_answers_and_error_rows(user, monkeypatch):
    await evaluated_summary_fleet(user, monkeypatch, *PICK_FLEET)

    await pick(user, "(no answers)")
    assert result_hosts(user) == ["multi", "multi"]

    await pick(user, "error: transport")
    assert result_hosts(user) == ["down"]


async def test_pick_and_filter_combine(user, monkeypatch):
    await evaluated_summary_fleet(user, monkeypatch, *PICK_FLEET)
    await pick(user, "(no answers)")

    await search(user, "debian")
    assert result_hosts(user) == ["multi", "multi"]

    await search(user, "red hat")
    assert result_hosts(user) == []


async def test_pick_summary_line(user, monkeypatch):
    await evaluated_summary_fleet(user, monkeypatch, *PICK_FLEET)

    await pick(user, "Linux Debian 11")

    assert summary_text(user) == 'Showing 3 of 5 results: hosts answering "Linux Debian 11"'


async def test_pick_does_not_auto_expand(user, monkeypatch):
    await evaluated_summary_fleet(user, monkeypatch, *PICK_FLEET)

    await pick(user, "Linux Debian 11")

    assert result_hosts(user) == ["deb", "multi", "multi"]
    assert results_table(user).props["expanded"] == []


async def test_pick_keeps_what_the_user_expanded(user, monkeypatch):
    await evaluated_summary_fleet(user, monkeypatch, *PICK_FLEET)
    [rh] = [r for r in results_table(user).rows if r["host"] == "rh"]
    user.find(marker="results").trigger("update:expanded", [rh["id"]])

    await pick(user, "Linux Debian 11")
    assert "rh" not in result_hosts(user)
    await pick(user, "Linux Debian 11")

    assert results_table(user).props["expanded"] == [rh["id"]]


async def rerun(user, monkeypatch, *results):
    monkeypatch.setattr(app_module, "stream_results", stream_of(*results))
    monkeypatch.setattr(app_module, "total_work", lambda targets, opts: len(results))
    user.find(marker="evaluate").click()
    await user.should_see(f"{len(results)} / {len(results)}")
    await asyncio.sleep(0.1)


async def test_pick_survives_evaluate(user, monkeypatch):
    await evaluated_summary_fleet(user, monkeypatch, *PICK_FLEET)
    await pick(user, "Linux Debian 11")

    await rerun(user, monkeypatch, *PICK_FLEET)

    assert result_hosts(user) == ["deb", "multi", "multi"]
    await user.should_see(marker="results-pick")


async def test_pick_with_no_matching_host_after_a_rerun_says_so(user, monkeypatch):
    await evaluated_summary_fleet(user, monkeypatch, *PICK_FLEET)
    await pick(user, "Linux Debian 11")

    await rerun(user, monkeypatch, result("a", ["Windows 11"]), result("b", ["Windows 11"]))

    assert result_hosts(user) == []
    assert summary_text(user).startswith("Showing 0 of 2 results")
    await user.should_see(marker="results-pick")


async def test_clear_removes_the_pick(user, monkeypatch):
    await evaluated_summary_fleet(user, monkeypatch, *PICK_FLEET)
    await pick(user, "Linux Debian 11")
    assert pick_chip(user).visible

    user.find(marker="clear").click()
    await rerun(user, monkeypatch, *PICK_FLEET)

    assert len(result_hosts(user)) == len(PICK_FLEET)
    await user.should_not_see(marker="results-pick")


async def test_export_ignores_the_pick(user, monkeypatch):
    exported: list[str] = []
    monkeypatch.setattr(
        UserDownload, "content", lambda self, payload, *a, **k: exported.append(payload)
    )
    await evaluated_summary_fleet(user, monkeypatch, *PICK_FLEET)
    await pick(user, "error: transport")
    assert result_hosts(user) == ["down"]

    user.find(marker="export").click()

    [payload] = exported
    assert len(json.loads(payload)) == len(PICK_FLEET)


# --- side panel and target count (issue 8) ------------------------------------------------


def side_panel(user):
    return hidden_element(user, "side-panel")


def toggle(user):
    return hidden_element(user, "targets-toggle")


async def count_reads(user, text: str, timeout: float = 2.0) -> None:
    """Wait for the header count to read ``text`` (inventory loads in the background).

    Read from the button, not with should_see, which also matches tooltip text.
    """
    for _ in range(int(timeout / 0.05)):
        if toggle(user).text == text:
            return
        await asyncio.sleep(0.05)
    assert toggle(user).text == text


async def test_side_panel_is_collapsed_by_default(inventory_user):
    await inventory_user.open("/")
    await count_reads(inventory_user, "2 systems")

    assert side_panel(inventory_user).value is False


async def test_toggle_opens_and_closes_the_side_panel(inventory_user):
    await inventory_user.open("/")
    await count_reads(inventory_user, "2 systems")

    inventory_user.find(marker="targets-toggle").click()
    assert side_panel(inventory_user).value is True

    inventory_user.find(marker="targets-toggle").click()
    assert side_panel(inventory_user).value is False


async def test_header_shows_the_logo_before_the_title(user):
    await user.open("/")

    [logo] = user.find(marker="header-logo").elements
    assert logo.props["src"]
    # Decorative: the title right after it says the same, so screen readers skip it.
    assert logo.props["alt"] == ""
    siblings = list(logo.parent_slot.children)
    title = next(e for e in siblings if getattr(e, "text", None) == app_module.TITLE)
    assert siblings.index(logo) < siblings.index(title)


async def test_header_shows_how_many_systems_will_be_targeted(inventory_user):
    await inventory_user.open("/")

    await count_reads(inventory_user, "2 systems")
    assert toggle(inventory_user).props["icon"] == "dns"
    assert "2 inventory" in hidden_element(inventory_user, "targets-tooltip").text


async def test_open_panel_shows_the_same_count(inventory_user):
    await inventory_user.open("/")
    await count_reads(inventory_user, "2 systems")

    text = hidden_element(inventory_user, "target-count").text
    assert "2 systems" in text
    assert "2 inventory" in text


async def test_target_count_updates_when_a_target_is_ticked(inventory_user):
    await inventory_user.open("/")
    await count_reads(inventory_user, "2 systems")

    inventory_user.find(marker="target-local").click()

    await count_reads(inventory_user, "3 systems")


def fake_arches(calls: list[str] | None = None, *, fail: bool = False):
    """A resolve_container_arches that, like the real one, asks only about unknown images."""

    async def resolve(form, *, known=None, **kwargs):
        if fail:
            raise RuntimeError("engine went away")
        known = known or {}
        images = [i.strip() for i in form.container_images.split(",") if i.strip()]
        if calls is not None:
            calls.extend(i for i in images if i not in known)
        return {image: list(known.get(image, ["x86_64", "arm64"])) for image in images}

    return resolve


async def after_arch_lookup() -> None:
    await asyncio.sleep(app_module.ARCH_LOOKUP_DEBOUNCE_S + 0.2)


async def test_target_count_with_auto_arch_is_looked_up_in_the_background(user, monkeypatch):
    monkeypatch.setattr(app_module, "resolve_container_arches", fake_arches())
    await user.open("/")
    await count_reads(user, "0 systems")

    user.find(marker="target-container").click()

    await count_reads(user, "≥ 1 system")
    await after_arch_lookup()
    await count_reads(user, "2 systems")


async def test_arch_lookup_runs_once_per_image(user, monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(app_module, "resolve_container_arches", fake_arches(calls))
    await user.open("/")
    user.find(marker="target-container").click()
    await after_arch_lookup()

    user.find(marker="target-local").click()
    user.find(marker="target-local").click()
    await after_arch_lookup()
    assert calls == ["debian:11"]

    button(user, "container-images").set_value("ubuntu:24.04")
    await after_arch_lookup()

    assert calls == ["debian:11", "ubuntu:24.04"]
    await count_reads(user, "2 systems")


async def test_arch_lookup_failure_keeps_the_lower_bound(user, monkeypatch):
    monkeypatch.setattr(app_module, "resolve_container_arches", fake_arches(fail=True))
    await user.open("/")

    user.find(marker="target-container").click()
    await after_arch_lookup()

    await count_reads(user, "≥ 1 system")


async def test_evaluate_fills_the_arch_cache(user, monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(app_module, "resolve_container_arches", fake_arches(calls))
    monkeypatch.setattr(app_module, "stream_results", stream_of(result("a"), result("b")))
    await user.open("/")
    await set_text(user, "name of operating system")
    user.find(marker="target-container").click()

    user.find(marker="evaluate").click()  # before the background lookup's debounce
    await user.should_see("2 / 2")
    await after_arch_lookup()

    assert calls == ["debian:11"]
    await count_reads(user, "2 systems")


async def test_target_count_while_discovering_says_so(monkeypatch, tmp_path):
    release = threading.Event()
    written = tmp_path / "remote_clients.toml"

    def slow_discovery():
        release.wait(5)
        return written

    monkeypatch.setattr(app_module, "_inventory_path", None)
    monkeypatch.setattr(app_module, "find_inventory_path", lambda: None)
    monkeypatch.setattr(app_module, "find_or_discover_inventory", slow_discovery)
    monkeypatch.setattr(
        app_module,
        "load_inventory_targets",
        lambda path: InventoryLoad(path=path, targets=list(TWO_HOSTS), error=None),
    )

    async with user_simulation(root=app_module.root) as user:
        await user.open("/")
        try:
            await count_reads(user, "finding inventory …")
        finally:
            release.set()
        await count_reads(user, "2 systems")
        assert side_panel(user).value is False


async def test_panel_opens_itself_when_there_is_nothing_to_target(user):
    await user.open("/")

    await count_reads(user, "0 systems")
    assert side_panel(user).value is True
    assert toggle(user).props["icon"] == "warning"
    assert "Pick at least one target." in hidden_element(user, "target-count").text


async def test_panel_opens_itself_after_an_inventory_error(no_inventory, monkeypatch, tmp_path):
    bad = tmp_path / "remote_clients.toml"
    bad.write_text("= nope", encoding="utf-8")
    monkeypatch.setattr(app_module, "find_inventory_path", lambda: bad)

    async with user_simulation(root=app_module.root) as user:
        await user.open("/")
        await user.should_see(marker="inventory-error")
        await count_reads(user, "0 systems")
        assert side_panel(user).value is True


async def test_evaluate_with_no_targets_opens_the_panel(user):
    await user.open("/")
    await count_reads(user, "0 systems")
    user.find(marker="targets-toggle").click()
    assert side_panel(user).value is False

    user.find(marker="evaluate").click()

    await notified(user, "Pick at least one target")
    assert side_panel(user).value is True


async def test_session_only_count_says_bigfix_server(user):
    await user.open("/")

    await set_text(user, "number of bes computers")

    await count_reads(user, "BigFix server")
    assert toggle(user).props["icon"] == "dns"


async def test_main_column_is_full_width(user):
    await user.open("/")

    results_section = hidden_element(user, "results-section")
    assert "w-full" in results_section.parent_slot.parent.classes
    assert not [e for e in user.client.elements.values() if "w-2/3" in e.classes]


# --- inventory table (issue 8) --------------------------------------------------------------


async def inventory_page(monkeypatch, tmp_path, targets):
    monkeypatch.setattr(app_module, "_inventory_path", tmp_path / "remote_clients.toml")
    monkeypatch.setattr(
        app_module,
        "load_inventory_targets",
        lambda path: InventoryLoad(path=path, targets=list(targets), error=None),
    )


async def test_inventory_table_shows_kind_platform_and_qna(monkeypatch, tmp_path):
    await inventory_page(
        monkeypatch,
        tmp_path,
        [
            Target(kind="container", name="deb", image="debian:11", qna_version=["1.0", "2.0"]),
            Target(kind="ssh", name="lab-box"),
        ],
    )

    async with user_simulation(root=app_module.root) as user:
        await user.open("/")
        await user.should_see(marker="inventory")

        deb, box = inventory_table(user).rows
        assert (deb["kind"], deb["platform"], deb["qna"]) == ("container", "debian", "1.0, 2.0")
        assert (box["label"], box["platform"], box["qna"]) == ("lab-box", "?", "")


async def test_deselected_inventory_host_is_not_evaluated(inventory_user, monkeypatch):
    seen: dict = {}

    async def fake_stream(text, targets, opts, **upstream):
        seen["targets"] = targets
        yield ClientRelevanceResult(host="lab-a", transport="ssh", client_relevance=text)

    monkeypatch.setattr(app_module, "stream_results", fake_stream)
    await inventory_user.open("/")
    await inventory_user.should_see(marker="inventory")
    await set_text(inventory_user, "name of operating system")

    await deselect(inventory_user, 1)
    inventory_user.find(marker="evaluate").click()

    await inventory_user.should_see("1 / 1")
    assert seen["targets"] == [TWO_HOSTS[0]]


async def test_inventory_selection_updates_the_count_and_caption(inventory_user):
    await inventory_user.open("/")
    await inventory_user.should_see(marker="inventory")
    assert hidden_element(inventory_user, "inventory-count").text == "2 of 2 selected"

    await deselect(inventory_user, 1)

    assert hidden_element(inventory_user, "inventory-count").text == "1 of 2 selected"
    await count_reads(inventory_user, "1 system")


async def test_inventory_filter_only_with_many_hosts(inventory_user):
    await inventory_user.open("/")
    await inventory_user.should_see(marker="inventory")

    assert not [
        e for e in inventory_user.client.elements.values() if "inventory-filter" in e._markers
    ]


async def test_inventory_filter_with_many_hosts_filters_the_table(monkeypatch, tmp_path):
    hosts = [Target(kind="ssh", name=f"lab-{i}") for i in range(9)]
    await inventory_page(monkeypatch, tmp_path, hosts)

    async with user_simulation(root=app_module.root) as user:
        await user.open("/")
        await user.should_see(marker="inventory-filter")

        user.find(marker="inventory-filter").type("lab-3")
        await asyncio.sleep(0.05)

        assert inventory_table(user).props["filter"] == "lab-3"


# --- PR #12 review ----------------------------------------------------------------------------


class RecordingImages:
    """A container engine for the real resolve_container_arches: records what it's asked."""

    def __init__(self, published: dict[str, set[str]]) -> None:
        self.published = published
        self.asked: list[str] = []

    def local_tags(self):
        return []

    def platforms(self, image):
        self.asked.append(image)
        return self.published.get(image)


def fake_engine(monkeypatch) -> RecordingImages:
    images = RecordingImages({"debian:11": {"x86_64", "arm64"}, "ubuntu:24.04": {"arm64"}})
    monkeypatch.setattr(evaluation, "default_image_source", lambda: images)
    return images


async def test_refreshes_do_not_restart_an_arch_lookup_in_flight(user, monkeypatch):
    gate = asyncio.Event()
    calls: list[str] = []

    async def slow_registry(form, **kwargs):
        calls.append(form.container_images)
        await gate.wait()
        return {"debian:11": ["x86_64", "arm64"]}

    monkeypatch.setattr(app_module, "resolve_container_arches", slow_registry)
    await user.open("/")
    user.find(marker="target-container").click()
    await after_arch_lookup()
    assert calls == ["debian:11"]  # started, waiting on the registry

    # Typing and ticking refresh the count; none of that may restart the lookup.
    for _ in range(3):
        user.find(marker="target-local").click()
        await asyncio.sleep(0.1)
    await set_text(user, "name of operating system")
    await after_arch_lookup()
    gate.set()
    await asyncio.sleep(0.1)

    assert calls == ["debian:11"]
    await count_reads(user, "3 systems")


async def test_arch_lookup_asks_only_for_added_images(user, monkeypatch):
    images = fake_engine(monkeypatch)
    await user.open("/")
    user.find(marker="target-container").click()
    await after_arch_lookup()
    await count_reads(user, "2 systems")

    button(user, "container-images").set_value("debian:11, ubuntu:24.04")
    await after_arch_lookup()

    assert images.asked == ["debian:11", "ubuntu:24.04"]
    await count_reads(user, "3 systems")


async def test_evaluate_reuses_the_arches_the_count_looked_up(user, monkeypatch):
    images = fake_engine(monkeypatch)
    monkeypatch.setattr(app_module, "stream_results", stream_of(result("a"), result("b")))
    await user.open("/")
    await set_text(user, "name of operating system")
    user.find(marker="target-container").click()
    await after_arch_lookup()
    await count_reads(user, "2 systems")

    user.find(marker="evaluate").click()
    await user.should_see("2 / 2")

    assert images.asked == ["debian:11"]


async def test_arch_lookup_failure_is_shown_and_not_retried_on_every_change(user, monkeypatch):
    calls: list[int] = []

    async def broken(form, **kwargs):
        calls.append(1)
        raise RuntimeError("engine went away")

    monkeypatch.setattr(app_module, "resolve_container_arches", broken)
    await user.open("/")
    user.find(marker="target-container").click()
    await after_arch_lookup()

    user.find(marker="target-local").click()
    user.find(marker="target-local").click()
    await after_arch_lookup()

    assert calls == [1]
    await user.should_see("engine went away")  # in the activity log
    await count_reads(user, "≥ 1 system")


async def test_answer_summary_is_rebuilt_only_when_it_changes(user, monkeypatch):
    calls: list[int] = []
    real = AnswerTally.rows

    def counting(self, limit=app_module.TALLY_LIMIT):
        calls.append(1)
        return real(self, limit)

    monkeypatch.setattr(AnswerTally, "rows", counting)
    await evaluated_summary_fleet(user, monkeypatch, *PICK_FLEET)
    calls.clear()

    await search(user, "debian")
    await clear_search(user)
    [row] = [r for r in results_table(user).rows if r["host"] == "rh"]
    user.find(marker="results").trigger("update:expanded", [row["id"]])
    assert calls == []

    await pick(user, "Linux Debian 11")  # the picked row's highlight changes
    assert len(calls) == 1
