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
import os
import sys
import threading
from collections.abc import Awaitable, Callable
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
from nicegui.events import GenericEventArguments, ValueChangeEventArguments

from bigfix_relevance_debugger import __version__
from bigfix_relevance_debugger.analysis import AnalysisView, editor_decorations, summarize
from bigfix_relevance_debugger.evaluation import (
    ARCHES,
    AUTO_ARCH,
    EvalOptions,
    InventoryLoad,
    LevelResult,
    ResultRow,
    TargetForm,
    breakdown_qna_note,
    build_targets,
    choose_breakdown_target,
    load_inventory_targets,
    resolve_container_arches,
    results_summary,
    results_table_row,
    run_breakdown,
    stream_results,
    total_work,
    visible_results,
)
from bigfix_relevance_debugger.log_bridge import attach, detach
from bigfix_relevance_debugger.upstream.remote_client_relevance import find_or_discover_inventory

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


# Results table: one row per result; the rows come from results_table_row(),
# already in display order. Columns sort on click, but the default is ours.
_RESULT_COLUMNS = [
    {"name": "status", "label": "", "field": "status"},
    {"name": "host", "label": "Target", "field": "host", "align": "left", "sortable": True},
    {"name": "count", "label": "#", "field": "count", "align": "right", "sortable": True},
    {"name": "preview", "label": "Answers", "field": "preview", "align": "left"},
    {"name": "platform", "label": "Platform", "field": "platform", "align": "left"},
    # A host listing several qna versions has one row per version; this tells them apart.
    {"name": "qna", "label": "qna", "field": "qna", "align": "left"},
    {
        "name": "elapsed_ms",
        "label": "ms",
        "field": "elapsed_ms",
        "align": "right",
        "sortable": True,
    },
]

_RESULTS_HEADER_SLOT = r"""
<q-tr :props="props">
  <q-th auto-width />
  <q-th v-for="col in props.cols" :key="col.name" :props="props">{{ col.label }}</q-th>
</q-tr>
"""

# Answers come from the endpoint: interpolate ({{ }}), never v-html.
_RESULTS_BODY_SLOT = r"""
<q-tr :props="props" :class="props.row.ok ? '' : 'text-negative'">
  <q-td auto-width>
    <q-btn size="sm" flat round dense
           :icon="props.expand ? 'expand_less' : 'expand_more'"
           @click="props.expand = !props.expand" />
  </q-td>
  <q-td v-for="col in props.cols" :key="col.name" :props="props">
    <q-icon v-if="col.name === 'status'"
            :name="props.row.ok ? 'check_circle' : 'error'"
            :color="props.row.ok ? 'positive' : 'negative'" />
    <span v-else-if="col.name === 'preview'" class="bfrd-mono">{{ col.value }}</span>
    <span v-else>{{ col.value }}</span>
  </q-td>
</q-tr>
<q-tr v-show="props.expand" :props="props">
  <q-td colspan="100%">
    <div class="bfrd-mono" style="white-space: pre-wrap">{{ props.row.answers || '(no answers)' }}</div>
    <div class="text-caption opacity-70">
      {{ props.row.status }} · types: {{ props.row.types || '-' }} · qna {{ props.row.qna || '?' }}
    </div>
    <div v-if="props.row.error" class="text-negative" style="white-space: pre-wrap">{{ props.row.error }}</div>
    <pre v-if="props.row.raw" class="text-xs q-ma-none" style="white-space: pre-wrap">{{ props.row.raw }}</pre>
  </q-td>
</q-tr>
"""


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
    rows: list[ResultRow] = dataclasses.field(default_factory=list)
    """``results`` preformatted, kept in step so a new arrival converts only itself."""
    expanded: set[int] = dataclasses.field(default_factory=set)
    """Keys (indexes into ``rows``) of the results-table rows that are open."""
    filter_text: str = ""
    expanded_before_filter: set[int] | None = None
    """What the user had open before a search auto-opened the matches; restored after."""
    breakdown_text: str | None = None
    """The expression the breakdown on screen was run on."""

    @property
    def running(self) -> bool:
        return self.task is not None and not self.task.done()


async def root() -> None:
    """The single page. Built once per client (browser tab)."""
    ui.add_css(_CSS)
    ui.page_title(TITLE)
    state = _PageState()
    # Filled in by load_inventory() once the page is up: first-run discovery
    # can take minutes and must not hold up the page.
    inventory = InventoryLoad(path=None, targets=[], error=None)

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
                export_button = (
                    ui.button("Export JSON", icon="download").props("flat").mark("export")
                )
                export_button.disable()
                clear_button = (
                    ui.button("Clear", icon="clear_all")
                    .props("flat")
                    .mark("clear")
                    .tooltip("Clear the results and the breakdown")
                )
                clear_button.disable()
                breakdown_reason = ui.label().classes("text-caption opacity-70")

            progress = ui.linear_progress(0, show_value=False).classes("w-full")
            progress_label = ui.label().classes("text-caption")
            # The breakdown goes above the results so it's in view whichever
            # ran last; a long result table would push it off-screen otherwise.
            breakdown_box = ui.column().classes("w-full gap-0").mark("breakdown-section")
            with ui.column().classes("w-full gap-1").mark("results-section") as results_section:
                results_filter = (
                    ui.input(placeholder="Filter by answer or error text")
                    .props("clearable dense")
                    .classes("w-full")
                    .mark("results-filter")
                )
                results_summary_label = ui.label().classes("text-caption").mark("results-summary")
                results_table = (
                    ui.table(rows=[], columns=_RESULT_COLUMNS, row_key="id")
                    .props("dense flat wrap-cells")
                    .classes("w-full")
                    .mark("results")
                )
                results_table.add_slot("header", _RESULTS_HEADER_SLOT)
                results_table.add_slot("body", _RESULTS_BODY_SLOT)
                # Controlled expansion: once `expanded` is set, QTable only emits
                # update:expanded, so Python owns which rows are open.
                results_table.props["expanded"] = []
            results_section.set_visibility(False)

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
                    ui.select(
                        {AUTO_ARCH: "auto (every arch the image publishes)"}
                        | {a: a for a in ARCHES},
                        multiple=True,
                        label="Arch",
                    ).bind_value(state.form, "container_arches").classes("w-full")
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

                inventory_box = ui.column().classes("w-full gap-0")

            with ui.card().classes("w-full"):
                ui.label("Options").classes("text-subtitle1")
                ui.input("qna version (blank: latest)", placeholder="11.0.7.61").bind_value(
                    state, "qna_version"
                ).classes("w-full")
                with ui.row().classes("w-full no-wrap"):
                    ui.number("Timeout (s)", min=1, format="%.0f").bind_value(
                        state, "timeout_s"
                    ).classes("w-1/2").mark("option-timeout")
                    ui.number("Max parallel", min=1, precision=0).bind_value(
                        state, "max_parallel"
                    ).classes("w-1/2").mark("option-parallel")

            with ui.card().classes("w-full"):
                ui.label("Activity log").classes("text-subtitle1")
                activity = ui.log(max_lines=500).classes("w-full h-64 text-xs")

    client = ui.context.client

    # --- live analysis --------------------------------------------------------

    def set_highlight(span: tuple[int, int] | None) -> None:
        """Editor marks only; cheap enough for every hover event."""
        try:
            code.decorations = editor_decorations(state.view, span)
        except ValueError:
            logger.debug("decorations rejected", exc_info=True)

    def apply_analysis() -> None:
        view = state.view
        set_highlight(None)

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
        update_stale()
        update_buttons()

    def reanalyze() -> None:
        state.view = summarize(code.value or "")
        apply_analysis()

    debouncer = Debouncer(DEBOUNCE_S, reanalyze)
    code.on_value_change(lambda _: debouncer.trigger())

    def current_view() -> AnalysisView:
        """The analysis of what the editor shows *now*, not 300 ms ago.

        Called synchronously in the click handlers, before any await, so both
        buttons act on the same text the user sees.
        """
        debouncer.cancel()
        if state.view.text != (code.value or ""):
            reanalyze()
        return state.view

    def update_buttons() -> None:
        running = state.running
        evaluate_button.set_enabled(not running)
        breakdown_button.set_enabled(not running and state.view.can_breakdown)
        cancel_button.set_enabled(running)
        export_button.set_enabled(not running and bool(state.results))
        clear_button.set_enabled(not running and (bool(state.results) or has_breakdown()))
        reason = state.view.breakdown_disabled_reason
        breakdown_reason.set_text(reason if reason and state.view.text.strip() else "")

    def has_breakdown() -> bool:
        # Its children, not breakdown_text: that is None while the spinner shows.
        return bool(breakdown_box.default_slot.children)

    def breakdown_is_stale() -> bool:
        return state.breakdown_text is not None and state.breakdown_text != state.view.text

    def update_stale() -> None:
        # The table stays as evidence after an edit, but its spans belong to
        # the old text, so dim it and stop highlighting.
        stale = breakdown_is_stale()
        breakdown_box.classes(
            add="opacity-50" if stale else "", remove="" if stale else "opacity-50"
        )
        for element in breakdown_box.descendants():
            if "breakdown-stale" in element._markers:
                element.set_visibility(stale)

    def hover(span: tuple[int, int] | None) -> None:
        if span is not None and breakdown_is_stale():
            return
        set_highlight(span)

    # --- running ----------------------------------------------------------------

    async def collect() -> tuple[list[Target], EvalOptions] | None:
        try:
            image_arches = await resolve_container_arches(state.form)
            form = state.form
            explicit = form.local or form.container or form.ssh
            # Wait for the inventory only when the run needs it: hosts are
            # selected, or discovery is still going and nothing else is ticked
            # (its hosts will be the default). A first-run Local/Container/SSH
            # evaluation starts at once.
            if form.inventory_selected or (not inventory_task.done() and not explicit):
                if not inventory_task.done():
                    progress_label.set_text("waiting for inventory auto-discovery ...")
                # Shielded: cancelling a run must not cancel the shared load.
                await asyncio.shield(inventory_task)
                progress_label.set_text("")
            targets = build_targets(state.form, inventory.targets, image_arches)
            opts = EvalOptions(
                qna_version=state.qna_version or "",
                # None only when the field was cleared; 0 must reach validation.
                timeout_s=30.0 if state.timeout_s is None else float(state.timeout_s),
                max_parallel=8 if state.max_parallel is None else int(state.max_parallel),
            )
        except ValueError as exc:
            ui.notify(str(exc), type="warning")
            return None
        return targets, opts

    def start(job: Callable[[], Awaitable[None]]) -> None:
        """Run ``job`` as the page's one task, created before the click returns.

        Setting ``state.task`` synchronously is what disables the buttons
        before any await, so a second click can't start a second run.
        """
        if state.running:
            return

        async def wrapper() -> None:
            try:
                await job()
            except asyncio.CancelledError:
                if not client.is_deleted:
                    activity.push("cancelled")
                    ui.notify("Cancelled", type="info")
                raise
            except BigFixRelevanceError as exc:
                if not client.is_deleted:
                    ui.notify(str(exc), type="negative", multi_line=True)
                    activity.push(f"error: {exc}")
            except Exception as exc:
                logger.exception("run failed")
                if not client.is_deleted:
                    ui.notify(f"{type(exc).__name__}: {exc}", type="negative", multi_line=True)
            finally:
                # This task is still running while its own finally executes, so
                # `state.running` would read True; clear it before re-rendering.
                state.task = None
                if not client.is_deleted:
                    update_buttons()

        state.task = asyncio.create_task(wrapper())
        update_buttons()

    def evaluate() -> None:
        text = current_view().text

        async def job() -> None:
            collected = await collect()
            if collected is None:
                return
            targets, opts = collected
            total = total_work(targets, opts)
            done = 0
            state.results = []
            state.rows = []
            state.expanded = set()
            if state.expanded_before_filter is not None:
                state.expanded_before_filter = set()  # old keys mean nothing now
            render_results()
            progress.set_value(0)
            progress_label.set_text(f"0 / {total}")
            async for result in stream_results(text, targets, opts):
                state.results.append(result)
                state.rows.append(ResultRow.from_result(result))
                done += 1
                progress.set_value(done / total if total else 1)
                progress_label.set_text(f"{done} / {total}")
                render_results()

        start(job)

    def render_results() -> None:
        """Re-sort the results table; called per streamed result, so it stays cheap."""
        query = state.filter_text
        order = visible_results(state.rows, query)
        if query.strip():
            # A search opens the matching hosts, including ones streaming in.
            state.expanded |= set(order)
        results_table.rows = [results_table_row(i, state.rows[i]) for i in order]
        results_table.props["expanded"] = sorted(state.expanded)
        results_table.update()
        results_summary_label.set_text(results_summary(state.rows, len(order), query))
        results_section.set_visibility(bool(state.rows))

    def on_expanded(e: GenericEventArguments) -> None:
        state.expanded = {int(key) for key in e.args or []}
        results_table.props["expanded"] = sorted(state.expanded)
        results_table.update()

    results_table.on("update:expanded", on_expanded)

    def apply_filter(e: ValueChangeEventArguments) -> None:
        # A cleared Quasar input sends None.
        state.filter_text = e.value or ""
        if state.filter_text.strip():
            if state.expanded_before_filter is None:
                state.expanded_before_filter = state.expanded
            state.expanded = set(visible_results(state.rows, state.filter_text))
        elif state.expanded_before_filter is not None:
            state.expanded = state.expanded_before_filter
            state.expanded_before_filter = None
        render_results()

    results_filter.on_value_change(apply_filter)

    def breakdown() -> None:
        view = current_view()
        if not view.can_breakdown:
            ui.notify(view.breakdown_disabled_reason or "cannot break down", type="warning")
            return

        async def job() -> None:
            collected = await collect()
            if collected is None:
                return
            targets, opts = collected
            # Every inventory host is selected by default, so pick the best fit
            # rather than ask; selecting a single target is the override.
            choice = choose_breakdown_target(view, targets)
            target = choice.target
            note = breakdown_qna_note(target)
            reason = f"{choice.reason}; {note}" if note else choice.reason
            state.breakdown_text = None
            breakdown_box.clear()
            update_stale()
            with breakdown_box:
                ui.label(f"Breakdown on {target.label} ...").classes("text-subtitle1")
                ui.label(reason).classes("text-caption opacity-70").mark("breakdown-reason")
                ui.spinner()
            levels = await run_breakdown(view, target, opts)
            breakdown_box.clear()
            state.breakdown_text = view.text
            with breakdown_box:
                ui.label(
                    "The expression has changed since this breakdown ran; "
                    "run Breakdown again to update it."
                ).classes("text-caption text-warning").mark("breakdown-stale")
                _breakdown_table(target, levels, reason=reason, on_hover=hover)
            update_stale()

        start(job)

    def cancel() -> None:
        if state.task is not None:
            state.task.cancel()

    def clear_all() -> None:
        """Clear results, breakdown, progress and search; the activity log stays."""
        if state.running:
            return
        state.results = []
        state.rows = []
        state.expanded = set()
        state.expanded_before_filter = None
        state.breakdown_text = None
        results_filter.set_value("")  # fires apply_filter only if it changed
        render_results()
        breakdown_box.clear()
        update_stale()
        set_highlight(None)
        progress.set_value(0)
        progress_label.set_text("")
        update_buttons()

    def export() -> None:
        payload = json.dumps(results_to_dicts(state.results), indent=2)
        ui.download.content(payload, "relevance-results.json", "application/json")

    evaluate_button.on_click(evaluate)
    breakdown_button.on_click(breakdown)
    cancel_button.on_click(cancel)
    export_button.on_click(export)
    clear_button.on_click(clear_all)

    # --- activity log -------------------------------------------------------------

    handler = attach(activity.push, _log_level, loop=asyncio.get_running_loop())
    # on_delete, not on_disconnect: a brief websocket drop reconnects to the
    # same client, and its log should keep flowing.
    client.on_delete(lambda: detach(handler))
    client.on_delete(debouncer.cancel)
    client.on_delete(cancel)

    # --- inventory -------------------------------------------------------------------

    def render_inventory(discovering: bool = False) -> None:
        inventory_box.clear()
        with inventory_box:
            if discovering:
                with ui.row().classes("items-center gap-2").mark("inventory-discovering"):
                    ui.spinner(size="sm")
                    ui.label(
                        "No remote_clients.toml found: running auto-discovery, which "
                        "writes ~/.bigfix/remote_clients.toml (progress in the activity log)."
                    ).classes("text-caption")
            elif inventory.error:
                where = f" {inventory.path}" if inventory.path else ""
                ui.label(f"Inventory{where}: {inventory.error}").classes(
                    "text-negative text-caption"
                ).mark("inventory-error")
            elif inventory.targets:
                ui.select(
                    {i: _target_option(t) for i, t in enumerate(inventory.targets)},
                    multiple=True,
                    clearable=True,
                    label=f"Inventory ({inventory.path})",
                ).bind_value(state.form, "inventory_selected").classes("w-full").mark("inventory")
            else:
                ui.label("No inventory hosts; tick a target above.").classes(
                    "text-caption opacity-70"
                ).mark("inventory-none")

    async def load_inventory() -> None:
        nonlocal inventory
        try:
            path = _inventory_path or find_inventory_path()
            if path is None:
                render_inventory(discovering=True)
                path = await asyncio.to_thread(_discover_inventory_once)
            inventory = load_inventory_targets(path)
        except asyncio.CancelledError:
            # The page closed mid-discovery. The worker thread finishes on its
            # own (its file write is still wanted); there is no page to update.
            return
        except Exception as exc:
            logger.debug("inventory discovery failed", exc_info=True)
            inventory = InventoryLoad(path=None, targets=[], error=f"auto-discovery failed: {exc}")
        # Evaluate runs on the whole inventory unless hosts are deselected.
        state.form.inventory_selected = list(range(len(inventory.targets)))
        render_inventory()

    # Started after the log handler is attached, so discovery's progress shows.
    inventory_task = asyncio.create_task(load_inventory())
    client.on_delete(inventory_task.cancel)

    reanalyze()


# Two tabs opened on a first run must not both run discovery; the second
# waits, then finds the file the first one wrote.
_discovery_lock = threading.Lock()


def _discover_inventory_once() -> Path | None:
    with _discovery_lock:
        return find_or_discover_inventory()


def _target_option(target: Target) -> str:
    return f"{target.label} ({target.kind})"


def _breakdown_table(
    target: Target,
    levels: list[LevelResult],
    *,
    reason: str,
    on_hover: Callable[[tuple[int, int] | None], None],
) -> None:
    ui.label(f"Breakdown on {target.label}").classes("text-subtitle1")
    ui.label(reason).classes("text-caption opacity-70").mark("breakdown-reason")
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
            with ui.column().classes(f"gap-0 {row_classes}").mark("breakdown-level") as level_cell:
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


WINDOW_SIZE = (1400, 900)


def native_window_problem() -> str | None:
    """Why a native window can't open here, or None if it can.

    pywebview ships with this package, but on Linux it brings no GUI toolkit
    of its own: it needs PyGObject (GTK) or Qt bindings, and a display.
    """
    if importlib.util.find_spec("webview") is None:
        return "pywebview isn't installed"
    if sys.platform.startswith("linux"):
        if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
            return "no display (DISPLAY / WAYLAND_DISPLAY unset)"
        if importlib.util.find_spec("gi") is None and importlib.util.find_spec("qtpy") is None:
            return (
                "pywebview needs GTK (PyGObject) or Qt on Linux, e.g. "
                "uvx --with 'pywebview[qt]' bigfix-relevance-debugger"
            )
    return None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bigfix-relevance-debugger",
        description=TITLE + ": live analysis, evaluation and per-level breakdown. "
        "Opens in a native window by default.",
    )
    parser.add_argument(
        "--host", default="127.0.0.1", help="interface to bind (default: 127.0.0.1)"
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="port (default: a free port for the native window, 8080 otherwise)",
    )
    parser.add_argument(
        "--browser",
        action="store_true",
        help="open in a browser tab instead of a native window",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="open no window at all; just serve (browse to the printed URL)",
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

    native = not (args.browser or args.no_browser)
    if native:
        problem = native_window_problem()
        if problem is not None:
            logger.warning("native window unavailable (%s); opening a browser tab", problem)
            native = False

    kwargs: dict[str, Any] = {
        "host": args.host,
        "title": TITLE,
        "native": native,
        "show": not native and not args.no_browser,
        # Follow the OS light/dark setting.
        "dark": None,
        # A console script has no source file to watch; reload would re-exec it.
        "reload": False,
        "show_welcome_message": True,
    }
    if native:
        kwargs["window_size"] = WINDOW_SIZE
    if args.port is not None:
        kwargs["port"] = args.port
    ui.run(root, **kwargs)
