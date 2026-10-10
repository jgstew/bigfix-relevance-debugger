"""Tests for app.py: CLI entry point, debouncing, and the page via NiceGUI's simulated user.

The page tests use ``user_simulation`` (no browser, no server socket) and
replace the evaluation entry points app.py imports, so nothing reaches
Docker, qna or SSH.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import AsyncIterator

import pytest
from bigfix_remote_client_relevance import ClientRelevanceResult, InventoryError, Target
from nicegui import ui
from nicegui.testing.user_simulation import user_simulation

from bigfix_relevance_debugger import app as app_module
from bigfix_relevance_debugger.app import Debouncer, build_parser, main
from bigfix_relevance_debugger.evaluation import (
    InventoryLoad,
    LevelResult,
    ProbeView,
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
    assert "operating system" in row["suggestions"]


async def test_live_analysis_decorates_editor(user):
    await user.open("/")

    await set_text(user, "name of ) x")

    assert {"kind": "mark", "from": 8, "to": 9, "class": "bfrd-parse-error"} in editor(
        user
    ).decorations


async def test_evaluate_without_targets_warns(user):
    await user.open("/")

    user.find(marker="evaluate").click()

    await user.should_see("Pick at least one target")


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

    await user.should_see("Linux Debian 11")
    await user.should_see("1 / 1")
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
        [select] = user.find(marker="inventory").elements
        assert list(select.options.values()) == ["shared-lab (ssh)"]
        assert str(home / ".bigfix" / "remote_clients.toml") in select.props["label"]


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

    [select] = inventory_user.find(marker="inventory").elements
    assert select.value == [0, 1]


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


async def test_inventory_picker_is_clearable(inventory_user):
    await inventory_user.open("/")

    [select] = inventory_user.find(marker="inventory").elements
    assert select.props.get("clearable") is True


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
        [select] = user.find(marker="inventory").elements
        assert select.value == [0, 1]


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
