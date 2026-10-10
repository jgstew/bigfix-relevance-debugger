"""NiceGUI page and console entry point. UI wiring only.

Logic lives in analysis.py (static analysis), evaluation.py (targets,
streaming, breakdown) and log_bridge.py (library logs -> activity panel).
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import importlib.util
import inspect
import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from bigfix_remote_client_relevance import (
    BigFixRelevanceError,
    ClientRelevanceResult,
    Target,
    find_inventory_path,
    results_to_dicts,
)
from nicegui import ui

from bigfix_relevance_debugger import __version__
from bigfix_relevance_debugger.analysis import AnalysisView, editor_decorations, summarize
from bigfix_relevance_debugger.evaluation import (
    ARCHES,
    EvalOptions,
    LevelResult,
    ResultRow,
    TargetForm,
    build_targets,
    load_inventory_targets,
    run_breakdown,
    stream_results,
    total_work,
)
from bigfix_relevance_debugger.log_bridge import attach, detach

logger = logging.getLogger(__name__)

TITLE = "BigFix Relevance Debugger"
DEBOUNCE_S = 0.3

EXAMPLES = {
    "name of operating system": "name of operating system",
    "files bigger than 1000 bytes in /etc": (
        'names of files whose (size of it > 1000) of folder "/etc"'
    ),
    "same, missing folder": 'names of files whose (size of it > 1000) of folder "/nope"',
    "typo (lint finding)": "name of operating sytem",
    "parse error": "name of (operating system",
}

# Set by main() from -v, read by each page for its activity-log handler.
_log_level = logging.INFO
# Set by main() from --inventory. None means the library's own search
# (./remote_clients.toml, ~/.bigfix/remote_clients.toml, the all-users config
# dir), so this app and the bigfix-remote-client-relevance CLI share one file.
_inventory_path: Path | None = None

_CSS = """
body.body--light { background: #fafafa; }
body.body--dark { background: #121212; }
.bfrd-parse-error { text-decoration: wavy underline red; background: rgba(255,0,0,.12); }
.bfrd-error { text-decoration: wavy underline red; }
.bfrd-warning { text-decoration: wavy underline orange; }
.bfrd-level { background: rgba(255, 213, 79, .55); border-radius: 2px; }
.bfrd-mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
"""

_OUTCOME_COLOURS = {
    "count": "positive",
    "empty-or-error": "warning",
    "not-evaluable": "negative",
    "no-context": "grey",
    "error": "negative",
}

_OUTCOME_HINTS = {
    "count": "This level produced objects.",
    "empty-or-error": (
        "0: produced nothing -- or errored in a plural context, which relevance "
        "flattens to empty. The probe can't tell these apart."
    ),
    "not-evaluable": (
        "-1: could not be evaluated, e.g. a singular reference to a nonexistent object."
    ),
    "no-context": "The probe answered nothing: its context produced no objects.",
    "error": "qna or the transport reported an error for this probe.",
}


class Debouncer:
    """Run ``callback`` once, ``delay`` seconds after the last ``trigger()``."""

    def __init__(self, delay: float, callback: Callable[[], Any]) -> None:
        self._delay = delay
        self._callback = callback
        self._task: asyncio.Task[None] | None = None

    def trigger(self) -> None:
        self.cancel()
        self._task = asyncio.create_task(self._fire())

    def cancel(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
        self._task = None

    async def _fire(self) -> None:
        await asyncio.sleep(self._delay)
        outcome = self._callback()
        if inspect.isawaitable(outcome):
            await outcome


@dataclasses.dataclass
class _PageState:
    """Per-browser-tab state; one instance per page build."""

    form: TargetForm = dataclasses.field(default_factory=TargetForm)
    qna_version: str = ""
    timeout_s: float = 30.0
    max_parallel: int = 8
    view: AnalysisView = dataclasses.field(default_factory=lambda: summarize(""))
    task: asyncio.Task[None] | None = None
    results: list[ClientRelevanceResult] = dataclasses.field(default_factory=list)

    @property
    def running(self) -> bool:
        return self.task is not None and not self.task.done()


async def root() -> None:
    """The single page. Built once per client (browser tab)."""
    ui.add_css(_CSS)
    ui.page_title(TITLE)
    state = _PageState()
    inventory = load_inventory_targets(_inventory_path or find_inventory_path())
    # Evaluate runs on the whole inventory unless hosts are deselected.
    state.form.inventory_selected = list(range(len(inventory.targets)))

    with ui.header().classes("items-center"):
        ui.label(TITLE).classes("text-h6")
        ui.label(f"v{__version__}").classes("text-caption opacity-70")

    with ui.row().classes("w-full no-wrap items-start gap-4"):
        # --- left: editor, analysis, results ---------------------------------
        with ui.column().classes("w-2/3 gap-2"):
            with ui.row().classes("w-full items-center"):
                ui.label("Client relevance").classes("text-subtitle1")
                ui.space()
                example = ui.select({v: k for k, v in EXAMPLES.items()}, label="Examples").classes(
                    "w-72"
                )

            code = ui.codemirror("", line_wrapping=True).classes("w-full h-40 border")
            code.mark("editor")
            example.on_value_change(lambda e: e.value and code.set_value(e.value))

            parse_banner = ui.label().classes("text-negative bfrd-mono")
            analysis_row = ui.row().classes("items-center gap-2")
            findings_box = ui.column().classes("w-full gap-1")

            with ui.row().classes("items-center gap-2"):
                evaluate_button = ui.button("Evaluate", icon="play_arrow").mark("evaluate")
                breakdown_button = ui.button("Breakdown", icon="account_tree").mark("breakdown")
                cancel_button = ui.button("Cancel", icon="stop", color="negative").mark("cancel")
                cancel_button.disable()
                export_button = ui.button("Export JSON", icon="download").props("flat")
                export_button.disable()
                breakdown_reason = ui.label().classes("text-caption opacity-70")

            progress = ui.linear_progress(0, show_value=False).classes("w-full")
            progress_label = ui.label().classes("text-caption")
            results_box = ui.column().classes("w-full gap-1")
            breakdown_box = ui.column().classes("w-full gap-0")

        # --- right: targets, options, log -----------------------------------
        with ui.column().classes("w-1/3 gap-2"):
            with ui.card().classes("w-full"):
                ui.label("Targets").classes("text-subtitle1")
                ui.checkbox("Local (this machine)").bind_value(state.form, "local").mark(
                    "target-local"
                )

                ui.checkbox("Container").bind_value(state.form, "container").mark(
                    "target-container"
                )
                with ui.column().classes("pl-8 w-full gap-0") as container_opts:
                    ui.input("Image(s), comma-separated").bind_value(
                        state.form, "container_images"
                    ).classes("w-full")
                    ui.select(list(ARCHES), multiple=True, label="Arch").bind_value(
                        state.form, "container_arches"
                    ).classes("w-full")
                    ui.checkbox("Rebuild image").bind_value(state.form, "rebuild_image")
                container_opts.bind_visibility_from(state.form, "container")

                ui.checkbox("SSH").bind_value(state.form, "ssh").mark("target-ssh")
                with ui.column().classes("pl-8 w-full gap-0") as ssh_opts:
                    ui.input("Host (~/.ssh/config alias works)").bind_value(
                        state.form, "ssh_host"
                    ).classes("w-full")
                    ui.input("User (blank: ssh config)").bind_value(state.form, "ssh_user").classes(
                        "w-full"
                    )
                    ui.checkbox("Become (sudo -n)").bind_value(state.form, "ssh_become")
                    ui.checkbox("Verify host key").bind_value(state.form, "ssh_verify_host_key")
                    ui.label("Key/agent auth only; no passwords.").classes(
                        "text-caption opacity-70"
                    )
                ssh_opts.bind_visibility_from(state.form, "ssh")

                if inventory.error:
                    ui.label(f"Inventory {inventory.path}: {inventory.error}").classes(
                        "text-negative text-caption"
                    ).mark("inventory-error")
                elif inventory.targets:
                    ui.select(
                        {i: _target_option(t) for i, t in enumerate(inventory.targets)},
                        multiple=True,
                        clearable=True,
                        label=f"Inventory ({inventory.path})",
                    ).bind_value(state.form, "inventory_selected").classes("w-full").mark(
                        "inventory"
                    )

            with ui.card().classes("w-full"):
                ui.label("Options").classes("text-subtitle1")
                ui.input("qna version (blank: latest)", placeholder="11.0.7.61").bind_value(
                    state, "qna_version"
                ).classes("w-full")
                with ui.row().classes("w-full no-wrap"):
                    ui.number("Timeout (s)", min=1, format="%.0f").bind_value(
                        state, "timeout_s"
                    ).classes("w-1/2")
                    ui.number("Max parallel", min=1, precision=0).bind_value(
                        state, "max_parallel"
                    ).classes("w-1/2")

            with ui.card().classes("w-full"):
                ui.label("Activity log").classes("text-subtitle1")
                activity = ui.log(max_lines=500).classes("w-full h-64 text-xs")

    # --- live analysis --------------------------------------------------------

    def apply_analysis(highlight: tuple[int, int] | None = None) -> None:
        view = state.view
        try:
            code.decorations = editor_decorations(view, highlight)
        except ValueError:
            logger.debug("decorations rejected", exc_info=True)
        if highlight is not None:
            return

        pe = view.parse_error
        parse_banner.set_text(
            f"Parse error at line {pe.line}, column {pe.column}: {pe.message}" if pe else ""
        )
        parse_banner.set_visibility(pe is not None and bool(view.text.strip()))

        analysis_row.clear()
        with analysis_row:
            ui.chip(f"dialect: {view.dialect}", icon="translate").props("outline dense")
            if view.type_summary:
                ui.chip(f"type: {view.type_summary}", icon="category").props("outline dense")
            for message in view.type_errors:
                ui.chip(message, icon="error", color="negative").props("outline dense")
            ui.chip(f"complexity: {view.complexity_score:g}", icon="speed").props("outline dense")
            ui.chip(f"cost: {view.evaluation_cost:g}", icon="payments").props("outline dense")
            for name in view.costly_inspectors:
                ui.chip(name, icon="warning", color="warning").props("dense")

        findings_box.clear()
        shown = [f for f in view.findings if f.code != "parse-error"]
        if shown:
            with findings_box:
                ui.table(
                    columns=[
                        {"name": "severity", "label": "Severity", "field": "severity"},
                        {"name": "code", "label": "Code", "field": "code"},
                        {"name": "line", "label": "Line", "field": "line"},
                        {
                            "name": "message",
                            "label": "Message",
                            "field": "message",
                            "align": "left",
                        },
                        {
                            "name": "suggestions",
                            "label": "Suggestions",
                            "field": "suggestions",
                            "align": "left",
                        },
                    ],
                    rows=[
                        {
                            "severity": f.severity,
                            "code": f.code,
                            "line": f.line,
                            "message": f.message,
                            "suggestions": ", ".join(f.suggestions),
                        }
                        for f in shown
                    ],
                ).props("dense flat").classes("w-full").mark("findings")
        update_buttons()

    def reanalyze() -> None:
        state.view = summarize(code.value or "")
        apply_analysis()

    debouncer = Debouncer(DEBOUNCE_S, reanalyze)
    code.on_value_change(lambda _: debouncer.trigger())

    def update_buttons() -> None:
        running = state.running
        evaluate_button.set_enabled(not running)
        breakdown_button.set_enabled(not running and state.view.can_breakdown)
        cancel_button.set_enabled(running)
        export_button.set_enabled(not running and bool(state.results))
        reason = state.view.breakdown_disabled_reason
        breakdown_reason.set_text(reason if reason and state.view.text.strip() else "")

    # --- running ----------------------------------------------------------------

    def collect() -> tuple[list[Target], EvalOptions] | None:
        try:
            targets = build_targets(state.form, inventory.targets)
            opts = EvalOptions(
                qna_version=state.qna_version or "",
                timeout_s=float(state.timeout_s or 30),
                max_parallel=int(state.max_parallel or 8),
            )
        except ValueError as exc:
            ui.notify(str(exc), type="warning")
            return None
        return targets, opts

    def start(job: Callable[[], Any]) -> None:
        async def wrapper() -> None:
            try:
                await job()
            except asyncio.CancelledError:
                activity.push("cancelled")
                ui.notify("Cancelled", type="info")
                raise
            except BigFixRelevanceError as exc:
                ui.notify(str(exc), type="negative", multi_line=True)
                activity.push(f"error: {exc}")
            except Exception as exc:
                logger.exception("run failed")
                ui.notify(f"{type(exc).__name__}: {exc}", type="negative", multi_line=True)
            finally:
                # This task is still running while its own finally executes, so
                # `state.running` would read True; clear it before re-rendering.
                state.task = None
                update_buttons()

        state.task = asyncio.create_task(wrapper())
        update_buttons()

    async def evaluate() -> None:
        collected = collect()
        if collected is None:
            return
        targets, opts = collected
        text = code.value or ""
        total = total_work(targets, opts)
        done = 0
        state.results = []
        results_box.clear()
        breakdown_box.clear()
        progress.set_value(0)
        progress_label.set_text(f"0 / {total}")

        async def job() -> None:
            nonlocal done
            async for result in stream_results(text, targets, opts):
                state.results.append(result)
                done += 1
                progress.set_value(done / total if total else 1)
                progress_label.set_text(f"{done} / {total}")
                with results_box:
                    _result_row(ResultRow.from_result(result))

        start(job)

    async def breakdown() -> None:
        view = state.view
        if not view.can_breakdown:
            ui.notify(view.breakdown_disabled_reason or "cannot break down", type="warning")
            return
        collected = collect()
        if collected is None:
            return
        targets, opts = collected
        if len(targets) == 1:
            [target] = targets
        else:
            # The breakdown runs one probe batch on one endpoint, but every
            # inventory host is selected by default, so ask rather than refuse.
            picked = await _choose_target(targets)
            if picked is None:
                return
            target = picked
        breakdown_box.clear()
        with breakdown_box:
            ui.label(f"Breakdown on {target.label} ...").classes("text-subtitle1")
            ui.spinner()

        async def job() -> None:
            levels = await run_breakdown(view, target, opts)
            breakdown_box.clear()
            with breakdown_box:
                _breakdown_table(target, levels, on_hover=hover)

        start(job)

    def hover(span: tuple[int, int] | None) -> None:
        apply_analysis(highlight=span)

    def cancel() -> None:
        if state.task is not None:
            state.task.cancel()

    def export() -> None:
        payload = json.dumps(results_to_dicts(state.results), indent=2)
        ui.download.content(payload, "relevance-results.json", "application/json")

    evaluate_button.on_click(evaluate)
    breakdown_button.on_click(breakdown)
    cancel_button.on_click(cancel)
    export_button.on_click(export)

    # --- activity log -------------------------------------------------------------

    handler = attach(activity.push, _log_level, loop=asyncio.get_running_loop())
    client = ui.context.client
    # on_delete, not on_disconnect: a brief websocket drop reconnects to the
    # same client, and its log should keep flowing.
    client.on_delete(lambda: detach(handler))
    client.on_delete(debouncer.cancel)
    client.on_delete(cancel)

    reanalyze()


async def _choose_target(targets: list[Target]) -> Target | None:
    """Ask which of several selected targets to break down on."""
    with ui.dialog() as dialog, ui.card():
        ui.label("Break down on which target?").classes("text-subtitle1")
        choice = ui.radio({i: _target_option(t) for i, t in enumerate(targets)}, value=0).mark(
            "breakdown-target-choice"
        )
        with ui.row().classes("w-full justify-end"):
            ui.button("Cancel", on_click=lambda: dialog.submit(None)).props("flat").mark(
                "breakdown-choice-cancel"
            )
            ui.button("Break down", on_click=lambda: dialog.submit(choice.value)).mark(
                "breakdown-go"
            )
    index = await dialog
    dialog.delete()
    return targets[index] if index is not None else None


def _target_option(target: Target) -> str:
    return f"{target.label} ({target.kind})"


def _result_row(row: ResultRow) -> None:
    icon, colour = ("check_circle", "positive") if row.ok else ("error", "negative")
    caption_bits = [row.qna_version_text, row.platform_text]
    if row.elapsed_ms is not None:
        caption_bits.append(f"{row.elapsed_ms} ms")
    with (
        ui.expansion(
            f"{row.host}: {row.answers_text if row.ok else row.error}",
            caption=" | ".join(b for b in caption_bits if b) or None,
            icon=icon,
        )
        .classes("w-full border rounded")
        .props(f"header-class=text-{colour} dense")
    ):
        with ui.grid(columns="auto 1fr").classes("gap-x-4 gap-y-1 text-sm"):
            ui.label("Status")
            ui.label(row.status)
            ui.label("Answers")
            with ui.column().classes("gap-0 bfrd-mono"):
                for answer in row.answers or ("(none)",):
                    ui.label(answer)
            ui.label("Types")
            ui.label(row.answer_types_text or "-")
            if row.error:
                ui.label("Error")
                ui.label(row.error).classes("text-negative")
        if row.raw_qna_output:
            ui.code(row.raw_qna_output).classes("w-full text-xs")


def _breakdown_table(
    target: Target,
    levels: list[LevelResult],
    *,
    on_hover: Callable[[tuple[int, int] | None], None],
) -> None:
    ui.label(f"Breakdown on {target.label}").classes("text-subtitle1")
    ui.label(
        "Objects each level produced, whole expression first. Hover a row to see it in "
        "the editor. 0 can mean empty or an error flattened to empty; -1 means not "
        "evaluable."
    ).classes("text-caption opacity-70")
    with ui.grid(columns="auto 1fr auto auto").classes("w-full gap-x-4 gap-y-0 items-center"):
        for header in ("#", "Level", "Objects", "Outcome"):
            ui.label(header).classes("text-bold")
        for lr in levels:
            cells: list[ui.element] = []
            row_classes = "bg-red-1 text-bold" if lr.is_first_failure else ""
            cells.append(ui.label(str(lr.level.index)).classes(row_classes))
            with ui.column().classes(f"gap-0 {row_classes}") as level_cell:
                label = ui.label(lr.level.label).classes("bfrd-mono")
                if lr.level.is_whole_expression:
                    label.tooltip("whole expression")
                if lr.filtered.error:
                    kind = lr.filtered.error_kind or "error"
                    ui.label(f"{kind}: {lr.filtered.error}").classes(
                        "text-caption text-negative"
                    ).mark("probe-error")
            cells.append(level_cell)
            cells.append(ui.label(lr.count_text).classes(row_classes))
            with ui.row().classes(f"items-center gap-1 {row_classes}") as outcome_cell:
                ui.badge(
                    lr.filtered.outcome, color=_OUTCOME_COLOURS.get(lr.filtered.outcome)
                ).tooltip(lr.filtered.error or _OUTCOME_HINTS.get(lr.filtered.outcome, ""))
                if lr.is_first_failure:
                    ui.icon("arrow_back", color="negative").mark("first-failure").tooltip(
                        "first level that goes empty / not evaluable"
                    )
            cells.append(outcome_cell)
            span = (lr.level.start, lr.level.end)
            for cell in cells:
                cell.on("mouseenter", lambda _e, s=span: on_hover(s))
                cell.on("mouseleave", lambda _e: on_hover(None))


# --- entry point -------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bigfix-relevance-debugger",
        description=TITLE + ": live analysis, evaluation and per-level breakdown.",
    )
    parser.add_argument(
        "--host", default="127.0.0.1", help="interface to bind (default: 127.0.0.1)"
    )
    parser.add_argument("--port", type=int, default=None, help="port (default: NiceGUI's)")
    parser.add_argument("--no-browser", action="store_true", help="don't open a browser window")
    parser.add_argument(
        "--native", action="store_true", help="open in a native window (needs pywebview)"
    )
    parser.add_argument(
        "--inventory",
        type=Path,
        default=None,
        metavar="PATH",
        help="remote_clients.toml to offer as targets (default: the same search as "
        "bigfix-remote-client-relevance, e.g. ~/.bigfix/remote_clients.toml)",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="show library DEBUG logs in the activity log"
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def main(argv: list[str] | None = None) -> None:
    global _log_level, _inventory_path
    args = build_parser().parse_args(argv)
    _log_level = logging.DEBUG if args.verbose else logging.INFO
    _inventory_path = args.inventory

    if args.native and importlib.util.find_spec("webview") is None:
        raise SystemExit(
            "--native needs pywebview: uvx --with pywebview bigfix-relevance-debugger --native"
        )

    kwargs: dict[str, Any] = {
        "host": args.host,
        "title": TITLE,
        "show": not args.no_browser,
        # Follow the OS light/dark setting.
        "dark": None,
        "native": args.native,
        # A console script has no source file to watch; reload would re-exec it.
        "reload": False,
        "show_welcome_message": True,
    }
    if args.port is not None:
        kwargs["port"] = args.port
    ui.run(root, **kwargs)
