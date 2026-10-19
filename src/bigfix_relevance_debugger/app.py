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
from platform import machine
from typing import Any, TypeVar

from bigfix_remote_client_relevance import (
    BigFixRelevanceError,
    ClientRelevanceResult,
    Target,
    find_inventory_path,
    results_to_dicts,
)
from bigfix_remote_client_relevance.inventory_paths import user_inventory_path
from nicegui import app as nicegui_app
from nicegui import background_tasks, ui
from nicegui.events import GenericEventArguments, ValueChangeEventArguments

from bigfix_relevance_debugger import __version__
from bigfix_relevance_debugger.analysis import (
    AUTO,
    CLIENT,
    DIALECT_MODES,
    SESSION,
    AnalysisView,
    editor_decorations,
    missing_fix_reason,
    suggestion_fix,
    summarize,
)
from bigfix_relevance_debugger.editor import RelevanceEditor
from bigfix_relevance_debugger.evaluation import (
    ARCHES,
    AUTO_ARCH,
    TALLY_LIMIT,
    AnswerTally,
    Cells,
    EvalOptions,
    InventoryLoad,
    LevelResult,
    NoTargetsError,
    ResultRow,
    SessionEvaluation,
    TargetForm,
    TargetPreview,
    breakdown_qna_note,
    build_targets,
    choose_breakdown_target,
    close_session_clients,
    expected_session_results,
    inventory_table_row,
    load_inventory_targets,
    needs_discovery,
    pick_label,
    preview_targets,
    resolve_container_arches,
    results_row_details,
    results_summary,
    results_table_row,
    run_breakdown,
    session_result_to_dict,
    session_row_wanted,
    stream_results,
    stream_session,
    tally_table_row,
    total_work,
    visible_results,
    waits_for_inventory,
)
from bigfix_relevance_debugger.log_bridge import attach, detach
from bigfix_relevance_debugger.upstream.remote_client_relevance import (
    discover_into,
    find_or_discover_inventory,
)

logger = logging.getLogger(__name__)

T = TypeVar("T")

TITLE = "BigFix Relevance Debugger"
LOGO = Path(__file__).with_name("logo.svg")
"""bigfix-relevance-analyzer's light logo (docs/images/logo.svg): favicon, header and Dock icon."""
WINDOW_ICONS = {
    "darwin": LOGO,
    # pywebview's Windows backend reads only an .ico (an SVG or PNG crashes it),
    # with BMP frames for .NET's System.Drawing.Icon. Both are made from the
    # analyzer's docs/images/logo-256.png.
    "win32": LOGO.with_suffix(".ico"),
}
"""The native window's icon by `sys.platform`; GTK and Qt get `logo.png`."""
APP_NAME = "Relevance Debugger"
"""The native window's app name in macOS's LaunchServices, "Python" otherwise."""
DEBOUNCE_S = 0.3
FILTER_DEBOUNCE_S = 0.2
"""Each filter change resends the results table, so wait for typing to pause."""
ARCH_LOOKUP_DEBOUNCE_S = 0.5
"""The target count looks up an ``auto`` arch once typing in the image field pauses."""
SIDE_PANEL_WIDTH = 460
INVENTORY_FILTER_MIN = 9
"""Hosts it takes for the inventory table to get a filter box."""


def name_macos_app(name: str) -> None:
    """Register this process with macOS as the app `name` (e.g. in Activity Monitor).

    LaunchServices takes the name from the bundle's ``CFBundleName``, which is
    Python.app's, when the Cocoa app starts, so call it before pywebview's
    ``webview.start``; later it changes nothing. The Dock's tooltip still says
    "Python": it reads the bundle on disk. Does nothing off macOS or without
    pyobjc (pywebview's macOS dependency).
    """
    if sys.platform != "darwin":
        return
    try:
        from Foundation import NSBundle  # pyright: ignore[reportAttributeAccessIssue]
    except ImportError:
        return
    NSBundle.mainBundle().infoDictionary()["CFBundleName"] = name


# NiceGUI opens the native window in a spawned process, which re-imports the
# console script (and so this module) as `__mp_main__` before it starts
# pywebview: the one chance to rename the app there. The window's process
# isn't marked as a child yet at that point (`parent_process()` is None), and
# `python -m` runs aren't re-imported at all.
if "__mp_main__" in sys.modules:
    name_macos_app(APP_NAME)

EXAMPLES = {
    "name of operating system": "name of operating system",
    "files bigger than 1000 bytes in /etc": (
        'names of files whose (size of it > 1000) of folder "/etc"'
    ),
    "same, missing folder": 'names of files whose (size of it > 1000) of folder "/nope"',
    "typo (lint finding)": "name of operating sytem",
    "parse error": "name of (operating system",
    "session: computers by name": "names of bes computers",
    "either dialect": "now",
}

_DIALECT_LABELS = {mode: mode.capitalize() for mode in DIALECT_MODES}

# Set by main() from -v, read by each page for its activity-log handler.
_log_level = logging.INFO
# Set by main() from --inventory. None means the library's own search
# (./remote_clients.toml, ~/.bigfix/remote_clients.toml, the all-users config
# dir), so this app and the bigfix-remote-client-relevance CLI share one file.
_inventory_path: Path | None = None

_CSS = """
body.body--light { background: #fafafa; }
body.body--dark { background: #121212; }
.bfrd-level { background: rgba(255, 213, 79, .55); border-radius: 2px; }
.bfrd-mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
/* Translucent, so the picked answer reads in light and dark mode alike. */
.bfrd-picked { background: rgba(25, 118, 210, .22); font-weight: bold; }
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
    <span v-else-if="col.name === 'host' && props.row.dialect === 'session'">
      {{ col.value }} <q-badge outline color="primary" label="session" />
    </span>
    <span v-else>{{ col.value }}</span>
  </q-td>
</q-tr>
<q-tr v-show="props.expand" :props="props">
  <q-td colspan="100%">
    <div class="bfrd-mono" style="white-space: pre-wrap">{{ props.row.answers }}</div>
    <div class="text-caption opacity-70">
      {{ props.row.status }} · types: {{ props.row.types || '-' }} · {{ props.row.detail }}
    </div>
    <div v-if="props.row.error" class="text-negative" style="white-space: pre-wrap">{{ props.row.error }}</div>
    <pre v-if="props.row.raw" class="text-xs q-ma-none" style="white-space: pre-wrap">{{ props.row.raw }}</pre>
  </q-td>
</q-tr>
"""

# Answer summary: one row per distinct answer, from tally_table_row(), already
# in display order (most systems first; no answers and errors last). Clicking
# a row picks it: the results then show only the systems it counted.
_ANSWER_COLUMNS = [
    {"name": "label", "label": "Answer", "field": "label", "align": "left"},
    {"name": "systems", "label": "Systems", "field": "systems", "align": "right"},
    {"name": "share", "label": "Share of systems", "field": "share", "align": "left"},
]

# Answers come from the endpoint: interpolate ({{ }}), never v-html.
_ANSWERS_BODY_SLOT = r"""
<q-tr :props="props" class="cursor-pointer"
      :class="[props.row.picked ? 'bfrd-picked' : '',
               props.row.kind === 'error' ? 'text-negative' : '',
               props.row.kind === 'empty' ? 'opacity-70' : '']"
      @click="() => $parent.$emit('pick', props.row.key)">
  <q-td key="label" :props="props" class="bfrd-mono">{{ props.row.label }}</q-td>
  <q-td key="systems" :props="props">{{ props.row.systems }}</q-td>
  <q-td key="share" :props="props" style="min-width: 10rem">
    <div class="row no-wrap items-center q-gutter-x-sm">
      <q-linear-progress :value="props.row.share" size="10px" rounded class="col"
                         :color="props.row.kind === 'error' ? 'negative' : 'primary'" />
      <span class="text-caption">{{ props.row.percent }}%</span>
    </div>
  </q-td>
</q-tr>
"""

# Findings: each "did you mean" lead is a button that applies it. Names and
# leads come from the user's text and the analyzer: interpolate, never v-html.
# The slot emits FIX_EVENT with these keys; apply_suggestion reads the same.
FIX_EVENT = "fix"
FIX_NAME = "name"
FIX_SUGGESTION = "suggestion"
_FINDINGS_SUGGESTIONS_SLOT = (
    r"""
<q-td :props="props">
  <q-btn v-for="fix in props.row.fixes" :key="fix.label"
         dense flat no-caps size="sm" color="primary" class="bfrd-mono" :label="fix.label"
         @click="() => $parent.$emit('@EVENT@', {@NAME@: fix.name, @SUGGESTION@: fix.suggestion})">
    <q-tooltip>Replace {{ fix.name }} with {{ fix.suggestion }}</q-tooltip>
  </q-btn>
</q-td>
""".replace("@EVENT@", FIX_EVENT)
    .replace("@NAME@", FIX_NAME)
    .replace("@SUGGESTION@", FIX_SUGGESTION)
)

# Three columns fit the side panel; kind and platform share one.
_INVENTORY_COLUMNS = [
    {"name": "label", "label": "Target", "field": "label", "align": "left", "sortable": True},
    {"name": "about", "label": "Kind · platform", "field": "about", "align": "left"},
    {"name": "qna", "label": "qna", "field": "qna", "align": "left"},
]


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
    dialect_mode: str = AUTO
    """The dialect picker: auto, or the override."""
    view: AnalysisView = dataclasses.field(default_factory=lambda: summarize(""))
    task: asyncio.Task[None] | None = None
    results: list[ClientRelevanceResult] = dataclasses.field(default_factory=list)
    session_results: list[SessionEvaluation] = dataclasses.field(default_factory=list)
    rows: list[ResultRow] = dataclasses.field(default_factory=list)
    """``session_results`` and ``results`` preformatted, in arrival order, so a
    new arrival converts only itself."""
    expanded: set[int] = dataclasses.field(default_factory=set)
    """Keys (indexes into ``rows``) of the results-table rows that are open."""
    table_rows: list[Cells] = dataclasses.field(default_factory=list)
    """``results_table_row`` of each of ``rows``, built once when it arrives."""
    filter_text: str = ""
    """The filter the table shows; the input's value lands here after the debounce."""
    filter_seen: set[int] = dataclasses.field(default_factory=set)
    """Keys already shown under the current filter. Only rows new to it auto-open,
    so a match the user collapsed stays collapsed while results stream in."""
    expanded_before_filter: set[int] | None = None
    """What the user had open before a search auto-opened the matches; restored after."""
    breakdown_text: str | None = None
    """The expression the breakdown on screen was run on."""
    tally: AnswerTally = dataclasses.field(default_factory=AnswerTally)
    """Systems per answer over the whole run, for the answer summary."""
    pick: str | None = None
    """The answer-summary row clicked (its key): results show only the systems it
    counted. Kept across runs like the filter; Clear drops it."""
    image_arches: dict[str, list[str]] = dataclasses.field(default_factory=dict)
    """``auto`` arches looked up so far, per image: the count and Evaluate both reuse them."""
    arch_lookup_failed: set[str] = dataclasses.field(default_factory=set)
    """Images whose background lookup failed; only Evaluate tries those again."""
    inventory_ready: bool = False
    """The inventory load (or first-run discovery) has finished."""

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
        ui.image(LOGO).classes("w-8 h-8").props('no-spinner no-transition alt=""').mark(
            "header-logo"
        )
        ui.label(TITLE).classes("text-h6")
        ui.label(f"v{__version__}").classes("text-caption opacity-70")
        ui.space()
        # Always in view, so the count shows whether the side panel is open or not.
        targets_toggle = (
            ui.button(icon="dns").props("flat no-caps color=white").mark("targets-toggle")
        )
        with targets_toggle:
            targets_tooltip = ui.tooltip("").mark("targets-tooltip")

    # --- main: editor, analysis, results ---------------------------------------
    with ui.column().classes("w-full gap-2"):
        with ui.row().classes("w-full items-center"):
            ui.label("Relevance").classes("text-subtitle1")
            ui.space()
            example = ui.select({v: k for k, v in EXAMPLES.items()}, label="Examples").classes(
                "w-72"
            )

        code = RelevanceEditor("", line_wrapping=True).classes("w-full h-40 border")
        code.mark("editor")
        example.on_value_change(lambda e: e.value and code.set_value(e.value))

        parse_banner = ui.label().classes("text-negative bfrd-mono")
        analysis_row = ui.row().classes("items-center gap-2")
        findings_box = ui.column().classes("w-full gap-1")

        with ui.row().classes("w-full items-center gap-2 no-wrap"):
            dialect_select = (
                ui.select(_DIALECT_LABELS, value=AUTO, label="Dialect")
                .bind_value(state, "dialect_mode")
                .classes("w-32")
                .mark("dialect")
            )
            dialect_reason = ui.label().classes("text-caption opacity-70").mark("dialect-reason")

        with ui.row().classes("items-center gap-2"):
            evaluate_button = ui.button("Evaluate", icon="play_arrow").mark("evaluate")
            breakdown_button = ui.button("Breakdown", icon="account_tree").mark("breakdown")
            cancel_button = ui.button("Cancel", icon="stop", color="negative").mark("cancel")
            cancel_button.disable()
            export_button = ui.button("Export JSON", icon="download").props("flat").mark("export")
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
        # Then how many systems gave each answer, then the systems one by one.
        with ui.column().classes("w-full gap-1").mark("answers-section") as answers_section:
            answers_title = ui.label().classes("text-subtitle1").mark("answers-title")
            answers_table = (
                ui.table(rows=[], columns=_ANSWER_COLUMNS, row_key="key")
                .props("dense flat wrap-cells")
                .classes("w-full")
                .mark("answers")
            )
            answers_table.add_slot("body", _ANSWERS_BODY_SLOT)
            answers_more = ui.label().classes("text-caption opacity-70").mark("answers-more")
        answers_section.set_visibility(False)
        with ui.column().classes("w-full gap-1").mark("results-section") as results_section:
            ui.label("Results by system").classes("text-subtitle1").mark("results-title")
            pick_chip = (
                ui.chip(icon="filter_alt", removable=True)
                .props("dense outline color=primary")
                .mark("results-pick")
                .tooltip("Picked in the answer summary; click the row again or ✕ to show all")
            )
            pick_chip.set_visibility(False)
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

    # --- side panel: targets, options, log; collapsed unless there's nothing to run
    with (
        ui.right_drawer(value=False, bordered=True)
        .classes("q-pa-sm")
        .mark("side-panel") as side_panel
    ):
        side_panel.props["width"] = SIDE_PANEL_WIDTH  # a Number for Quasar, not a string
        with ui.column().classes("w-full gap-2"):
            target_count = ui.label().classes("text-caption").mark("target-count")
            with ui.card().classes("w-full"):
                ui.label("Targets").classes("text-subtitle1")
                # The inventory first: its hosts are what Evaluate runs on by default.
                inventory_box = ui.column().classes("w-full gap-0")
                ui.label("Add targets").classes("text-caption opacity-70 q-mt-sm")
                ui.checkbox("Local (this machine)").bind_value(state.form, "local").mark(
                    "target-local"
                ).on_value_change(lambda _: refresh_targets())

                ui.checkbox("Container").bind_value(state.form, "container").mark(
                    "target-container"
                ).on_value_change(lambda _: refresh_targets())
                with ui.column().classes("pl-8 w-full gap-0") as container_opts:
                    ui.input("Image(s), comma-separated").bind_value(
                        state.form, "container_images"
                    ).classes("w-full").mark("container-images").on_value_change(
                        lambda _: refresh_targets()
                    )
                    ui.select(
                        {AUTO_ARCH: "auto (every arch the image publishes)"}
                        | {a: a for a in ARCHES},
                        multiple=True,
                        label="Arch",
                    ).bind_value(state.form, "container_arches").classes("w-full").on_value_change(
                        lambda _: refresh_targets()
                    )
                    ui.checkbox("Rebuild image").bind_value(state.form, "rebuild_image")
                container_opts.bind_visibility_from(state.form, "container")

                ui.checkbox("SSH").bind_value(state.form, "ssh").mark("target-ssh").on_value_change(
                    lambda _: refresh_targets()
                )
                with ui.column().classes("pl-8 w-full gap-0") as ssh_opts:
                    ui.input("Host (~/.ssh/config alias works)").bind_value(
                        state.form, "ssh_host"
                    ).classes("w-full").on_value_change(lambda _: refresh_targets())
                    ui.input("User (blank: ssh config)").bind_value(state.form, "ssh_user").classes(
                        "w-full"
                    )
                    ui.checkbox("Become (sudo -n)").bind_value(state.form, "ssh_become")
                    ui.checkbox("Verify host key").bind_value(state.form, "ssh_verify_host_key")
                    ui.label("Key/agent auth only; no passwords.").classes(
                        "text-caption opacity-70"
                    )
                ssh_opts.bind_visibility_from(state.form, "ssh")

            with ui.card().classes("w-full"):
                ui.label("Options").classes("text-subtitle1")
                ui.input("qna version (blank: latest)", placeholder="11.0.7.61").bind_value(
                    state, "qna_version"
                ).classes("w-full").on_value_change(lambda _: refresh_targets())
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

        mode = "auto" if view.dialect_mode == AUTO else "override"
        dialect_reason.set_text(f"{mode}: {view.dialect_reason}")

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
        if view.autofix is not None:
            with findings_box:
                edits = view.autofix.applied
                with (
                    ui.button("Fix all safe issues", icon="auto_fix_high", on_click=fix_all)
                    .props("dense outline no-caps")
                    .mark("fix-all")
                ):
                    ui.tooltip(f"Apply the analyzer's {edits} safe edit{'s' if edits != 1 else ''}")
        if shown:
            with findings_box:
                table = (
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
                                # Wrap: a long message must not push the
                                # suggestion buttons out of view.
                                "style": "white-space: normal; min-width: 16rem",
                            },
                            {
                                "name": "suggestions",
                                "label": "Suggestions",
                                "field": "fixes",
                                "align": "left",
                                "style": "white-space: normal",
                            },
                        ],
                        rows=[
                            {
                                "severity": f.severity,
                                "code": f.code,
                                "line": f.line,
                                "message": f.message,
                                "fixes": [
                                    {
                                        "name": fix.name,
                                        "suggestion": fix.suggestion,
                                        "label": fix.label,
                                    }
                                    for fix in f.fixes
                                ],
                            }
                            for f in shown
                        ],
                    )
                    .props("dense flat")
                    .classes("w-full")
                    .mark("findings")
                )
                table.add_slot("body-cell-suggestions", _FINDINGS_SUGGESTIONS_SLOT)
                table.on(FIX_EVENT, apply_suggestion)
        update_stale()
        update_buttons()
        refresh_targets()

    def reanalyze() -> None:
        state.view = summarize(code.value or "", state.dialect_mode)
        apply_analysis()

    def fix_all() -> None:
        # The button belongs to the last analysis; fix what the editor shows now.
        view = current_view()
        if view.autofix is None:
            ui.notify("Nothing to fix in the current text.", type="info")
            return
        code.set_value(view.autofix.text)

    def apply_suggestion(e: GenericEventArguments) -> None:
        name, suggestion = e.args[FIX_NAME], e.args[FIX_SUGGESTION]
        # The table belongs to the last analysis; fix what the editor shows now.
        view = current_view()
        text = suggestion_fix(view, name, suggestion)
        if text is None:
            ui.notify(missing_fix_reason(view, name, suggestion), type="warning")
            return
        code.set_value(text)

    async def settle() -> None:
        """After typing stops: the page's analysis, then the editor's squiggles."""
        reanalyze()
        await code.publish(state.view.lint_dialect)

    def refresh_editor() -> None:
        """The editor's server catches up in the background; the page doesn't wait.

        Cheap when nothing changed, so call it whenever the debounce is cut short.
        """
        background_tasks.create(code.publish(state.view.lint_dialect), name="editor-publish")

    def on_dialect_change() -> None:
        reanalyze()
        refresh_editor()

    debouncer = Debouncer(DEBOUNCE_S, settle)
    code.on_value_change(lambda _: debouncer.trigger())
    dialect_select.on_value_change(on_dialect_change)

    def current_view() -> AnalysisView:
        """The analysis of what the editor shows *now*, not 300 ms ago.

        Called synchronously in the click handlers, before any await, so both
        buttons act on the same text the user sees.
        """
        debouncer.cancel()
        view = state.view
        if view.text != (code.value or "") or view.dialect_mode != state.dialect_mode:
            reanalyze()
        # The cancelled debounce would have updated the editor's squiggles too.
        refresh_editor()
        return state.view

    def update_buttons() -> None:
        running = state.running
        evaluate_button.set_enabled(not running)
        breakdown_button.set_enabled(not running and state.view.can_breakdown)
        cancel_button.set_enabled(running)
        export_button.set_enabled(not running and bool(state.rows))
        clear_button.set_enabled(not running and (bool(state.rows) or has_breakdown()))
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

    # --- target count -----------------------------------------------------------

    def current_preview() -> TargetPreview:
        session = SESSION in state.view.run_dialects
        return preview_targets(
            state.form,
            inventory.targets,
            state.image_arches,
            qna_spec=EvalOptions(qna_version=state.qna_version or "").qna_spec,
            inventory_pending=not state.inventory_ready,
            client=CLIENT in state.view.run_dialects,
            session=session,
            discoverable=discoverable(),
            # Only asked when session relevance runs: it reads the config.
            session_servers=expected_session_results() if session else 1,
        )

    def refresh_targets() -> TargetPreview:
        """Show what Evaluate would run on now, in the header and the side panel."""
        show_inventory_count()
        preview = current_preview()
        problem = preview.problem is not None
        targets_toggle.set_text(preview.text)
        targets_toggle.props["icon"] = "warning" if problem else "dns"
        targets_toggle.props["color"] = "warning" if problem else "white"
        targets_toggle.update()
        targets_tooltip.set_text(preview.detail or preview.text)
        target_count.set_text(
            f"Evaluate runs on {preview.text}" + (f": {preview.detail}" if preview.detail else "")
        )
        target_count.classes(
            add="text-negative" if problem else "", remove="" if problem else "text-negative"
        )
        # Not while a lookup is in flight: the debounce would cancel it mid-query,
        # and its worker thread would keep asking the registry regardless.
        if arches_to_look_up(preview) and (arch_task is None or arch_task.done()):
            arch_debouncer.trigger()
        return preview

    def arches_to_look_up(preview: TargetPreview) -> list[str]:
        return [i for i in preview.unresolved_images if i not in state.arch_lookup_failed]

    arch_task: asyncio.Task[None] | None = None

    def start_arch_lookup() -> None:
        """After the debounce: look up the count's missing arches, in a task of its own."""
        nonlocal arch_task
        # A run looks them up itself and fills the cache.
        if state.running or (arch_task is not None and not arch_task.done()):
            return
        images = arches_to_look_up(current_preview())
        if not images:
            return
        arch_task = asyncio.create_task(look_up_arches(images))
        # Once it's done, so a change made meanwhile gets its own lookup.
        arch_task.add_done_callback(lambda _: client.is_deleted or refresh_targets())

    async def look_up_arches(images: list[str]) -> None:
        try:
            found = await resolve_container_arches(state.form, known=state.image_arches)
        except Exception as exc:
            # published_arches already falls back to this machine's arch when
            # no engine or registry answers, so this is unexpected; say so.
            state.arch_lookup_failed.update(images)
            message = (
                f"couldn't look up the arches of {', '.join(images)} for the target count: "
                f"{exc}; Evaluate will try again"
            )
            logger.warning(message, exc_info=True)
            activity.push(message)
            return
        state.image_arches.update(found)

    arch_debouncer = Debouncer(ARCH_LOOKUP_DEBOUNCE_S, start_arch_lookup)
    targets_toggle.on_click(side_panel.toggle)

    # --- running ----------------------------------------------------------------

    async def collect(
        *, client: bool = True, session: bool = False
    ) -> tuple[list[Target], EvalOptions] | None:
        """The run's targets and options, or None after saying what's wrong.

        ``client=False`` (session relevance only) needs no targets at all. With
        ``session`` too, having no client targets selected isn't an error: the
        run goes ahead as session relevance only, and says so. A selected
        target with a bad field (an SSH target with no host) still is.
        """
        try:
            opts = EvalOptions(
                qna_version=state.qna_version or "",
                # None only when the field was cleared; 0 must reach validation.
                timeout_s=30.0 if state.timeout_s is None else float(state.timeout_s),
                max_parallel=8 if state.max_parallel is None else int(state.max_parallel),
            )
            if not client:
                return [], opts
            image_arches = await resolve_container_arches(state.form, known=state.image_arches)
            state.image_arches.update(image_arches)
            refresh_targets()
            # Wait for the inventory only when the run needs it; the target
            # count applies the same rule.
            if waits_for_inventory(state.form, pending=not inventory_task.done()):
                if not inventory_task.done():
                    progress_label.set_text("waiting for inventory auto-discovery ...")
                # Shielded: cancelling a run must not cancel the shared load.
                await asyncio.shield(inventory_task)
                progress_label.set_text("")
            try:
                targets = build_targets(state.form, inventory.targets, image_arches)
            except NoTargetsError as exc:
                problem, targets = exc, []
                # Client-only relevance and no client target configured at all:
                # discover some first. With session too, run what is configured.
                if needs_discovery(
                    state.form, inventory.targets, discoverable=discoverable(), session=session
                ):
                    where = await discover_now()
                    try:
                        targets = build_targets(state.form, inventory.targets, image_arches)
                    except NoTargetsError:
                        problem = NoTargetsError(
                            f"Pick at least one target: auto-discovery found none to add to {where}."
                        )
                if not targets:
                    if not session:
                        raise problem from None
                    ui.notify(f"{problem} Running session relevance only.", type="info")
        except ValueError as exc:
            ui.notify(str(exc), type="warning")
            side_panel.show()  # targets and options both live there
            return None
        return targets, opts

    def in_page(work: Awaitable[T]) -> asyncio.Task[T]:
        """A task that can update this page: ``ui.notify`` and new elements need
        a slot, and NiceGUI keeps the slot stack per task, so a fresh task has
        none. (The simulated user's ``ui.notify`` needs none, which hides it.)
        """

        async def entered() -> T:
            with client:
                return await work

        return asyncio.create_task(entered())

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

        state.task = in_page(wrapper())
        update_buttons()

    def evaluate() -> None:
        view = current_view()
        text = view.text
        client = CLIENT in view.run_dialects
        session = SESSION in view.run_dialects

        async def job() -> None:
            collected = await collect(client=client, session=session)
            if collected is None:
                return
            targets, opts = collected
            # Deployments not discovered yet count as one per connection; the
            # session task corrects the total once it knows.
            session_expected = expected_session_results(opts=opts) if session else 0
            total = (total_work(targets, opts) if targets else 0) + session_expected
            done = 0
            state.results = []
            state.session_results = []
            state.rows = []
            state.table_rows = []
            state.tally = AnswerTally()
            state.expanded = set()
            state.filter_seen = set()
            if state.expanded_before_filter is not None:
                state.expanded_before_filter = set()  # old keys mean nothing now
            render_results()
            render_answers()
            progress.set_value(0)
            progress_label.set_text(f"0 / {total}")

            def show_progress() -> None:
                progress.set_value(done / total if total else 1)
                progress_label.set_text(f"{done} / {total}")

            def tick() -> None:
                nonlocal done
                done += 1
                show_progress()

            def arrived(row: ResultRow) -> None:
                state.table_rows.append(results_table_row(len(state.rows), row))
                state.tally.add(row)
                state.rows.append(row)
                tick()
                render_results()
                render_answers()

            # With no client targets the session row is all there is to show.
            session_optional = view.session_optional and bool(targets)

            async def run_session() -> None:
                nonlocal total, session_expected
                seen = 0
                # One result per deployment, as each answers.
                async for ev in stream_session(text, opts):
                    seen += 1
                    if seen > session_expected:
                        # More deployments than counted: grow the total now,
                        # so progress never reads past it.
                        total += 1
                        session_expected = seen
                    if not session_row_wanted(ev, optional=session_optional):
                        # Not an error row (it wasn't asked for), but say how to add one.
                        activity.push(f"session relevance skipped: {ev.error}")
                        ui.notify(f"Session relevance skipped. {ev.error}", type="info")
                        tick()
                        continue
                    state.session_results.append(ev)
                    outcome = (
                        f"{len(ev.answers)} answer(s)" if ev.ok else f"{ev.error_kind}: {ev.error}"
                    )
                    where = f"{ev.connection} ({ev.backend})" if ev.connection else ev.host
                    activity.push(f"session relevance on {where}: {outcome}")
                    arrived(ResultRow.from_session(ev))
                if seen < session_expected:
                    # Fewer: connections to one deployment counted once.
                    total -= session_expected - seen
                    show_progress()

            # The BigFix servers answer alongside qna, so a slow one holds up nothing.
            session_task = in_page(run_session()) if session else None
            try:
                if targets:
                    async for result in stream_results(text, targets, opts):
                        state.results.append(result)
                        arrived(ResultRow.from_result(result))
                if session_task is not None:
                    await session_task
            finally:
                if session_task is not None and not session_task.done():
                    session_task.cancel()

        start(job)

    def render_results() -> None:
        """Re-sort and resend the results table, once per streamed result.

        NiceGUI resends the whole ``rows`` prop, so collapsed rows are the
        prebuilt summary cells; only open rows add their answers and raw output.
        """
        query = state.filter_text
        order = visible_results(state.rows, query, state.pick)
        if query.strip():
            # A search opens matching hosts the first time they show (including
            # ones streaming in), then leaves them to the user.
            state.expanded |= set(order) - state.filter_seen
            state.filter_seen |= set(order)
        results_table.rows = [
            {**state.table_rows[i], **results_row_details(state.rows[i])}
            if i in state.expanded
            else state.table_rows[i]
            for i in order
        ]
        results_table.props["expanded"] = sorted(state.expanded)
        results_table.update()
        results_summary_label.set_text(results_summary(state.rows, len(order), query, state.pick))
        if state.pick is not None:
            pick_chip.set_text(f"Only: {pick_label(state.pick)}")
            pick_chip.value = True  # its ✕ set it False; a new pick shows it again
        pick_chip.set_visibility(state.pick is not None)
        results_section.set_visibility(bool(state.rows))

    def render_answers() -> None:
        """The answer summary: the whole run, whatever the filter hides.

        Only when it changes (a result arrives, a pick, Clear), not on every
        render_results: filtering and expanding leave it as it is.
        """
        tally = state.tally
        rows, hidden = tally.rows(TALLY_LIMIT)
        answers_table.rows = [tally_table_row(r, tally.systems, state.pick) for r in rows]
        answers_table.update()
        systems = f"{tally.systems} system" + ("" if tally.systems == 1 else "s")
        answers_title.set_text(f"Answers · {systems}")
        answers_more.set_text(
            f"+{hidden} more distinct answers; use the filter below to find one" if hidden else ""
        )
        answers_more.set_visibility(bool(hidden))
        # One system's summary would only repeat its row.
        answers_section.set_visibility(tally.systems >= 2)

    def on_expanded(e: GenericEventArguments) -> None:
        state.expanded = {int(key) for key in e.args or []}
        render_results()  # a newly opened row needs its details

    results_table.on("update:expanded", on_expanded)

    def on_pick(e: GenericEventArguments) -> None:
        # The picked row again un-picks. No auto-expand: the answer is known
        # already, and opening every match would resend all their raw output.
        key = str(e.args)
        state.pick = None if state.pick == key else key
        render_results()
        render_answers()  # the picked row's highlight

    answers_table.on("pick", on_pick)

    def on_pick_removed(e: ValueChangeEventArguments) -> None:
        if not e.value and state.pick is not None:
            state.pick = None
            render_results()
            render_answers()

    pick_chip.on_value_change(on_pick_removed)

    pending_filter = ""

    def apply_filter() -> None:
        state.filter_text = pending_filter
        state.filter_seen = set()
        if state.filter_text.strip():
            if state.expanded_before_filter is None:
                state.expanded_before_filter = state.expanded
            state.expanded = set()  # render_results opens the matches
        elif state.expanded_before_filter is not None:
            state.expanded = state.expanded_before_filter
            state.expanded_before_filter = None
        render_results()

    filter_debouncer = Debouncer(FILTER_DEBOUNCE_S, apply_filter)

    def on_filter_input(e: ValueChangeEventArguments) -> None:
        nonlocal pending_filter
        pending_filter = e.value or ""  # a cleared Quasar input sends None
        filter_debouncer.trigger()

    results_filter.on_value_change(on_filter_input)

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
        state.session_results = []
        state.rows = []
        state.expanded = set()
        state.expanded_before_filter = None
        state.breakdown_text = None
        results_filter.set_value("")
        filter_debouncer.cancel()  # cleared now, not after the debounce
        state.table_rows = []
        state.tally = AnswerTally()
        state.pick = None
        state.filter_text = ""
        state.filter_seen = set()
        render_results()
        render_answers()
        breakdown_box.clear()
        update_stale()
        set_highlight(None)
        progress.set_value(0)
        progress_label.set_text("")
        update_buttons()

    def export() -> None:
        # Session results first, as in the table; client entries keep the library's schema.
        entries = [session_result_to_dict(ev) for ev in state.session_results]
        payload = json.dumps(entries + results_to_dicts(state.results), indent=2)
        ui.download.content(payload, "relevance-results.json", "application/json")

    evaluate_button.on_click(evaluate)

    def submit() -> None:
        # Enter in the editor is the Evaluate button, which is disabled while running.
        if not state.running:
            evaluate()

    code.on_submit(submit)
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
    client.on_delete(code.close)
    client.on_delete(filter_debouncer.cancel)
    client.on_delete(arch_debouncer.cancel)
    client.on_delete(lambda: arch_task is not None and arch_task.cancel())
    client.on_delete(cancel)

    # --- inventory -------------------------------------------------------------------

    def show_inventory_count() -> None:
        """Refresh the inventory table's selection count; set while the table shows."""

    def render_inventory(discovering: bool = False) -> None:
        nonlocal show_inventory_count
        show_inventory_count = lambda: None  # until a table shows a count again
        inventory_box.clear()
        with inventory_box:
            if discovering:
                with ui.row().classes("items-center gap-2").mark("inventory-discovering"):
                    ui.spinner(size="sm")
                    ui.label(
                        f"No client targets configured: running auto-discovery, which "
                        f"writes {discovery_path()} (progress in the activity log)."
                    ).classes("text-caption")
            elif inventory.error:
                where = f" {inventory.path}" if inventory.path else ""
                ui.label(f"Inventory{where}: {inventory.error}").classes(
                    "text-negative text-caption"
                ).mark("inventory-error")
            elif inventory.targets:
                render_inventory_table()
            else:
                ui.label("No inventory hosts; tick a target above.").classes(
                    "text-caption opacity-70"
                ).mark("inventory-none")

    def render_inventory_table() -> None:
        rows = [inventory_table_row(i, t) for i, t in enumerate(inventory.targets)]
        chosen = set(state.form.inventory_selected or [])
        ui.label(f"Inventory · {inventory.path}").classes("text-caption").mark("inventory-title")
        host_filter = (
            ui.input(placeholder="Filter hosts")
            .props("dense clearable")
            .classes("w-full")
            .mark("inventory-filter")
            if len(rows) >= INVENTORY_FILTER_MIN
            else None
        )
        table = (
            ui.table(rows=rows, columns=_INVENTORY_COLUMNS, row_key="id", selection="multiple")
            # The panel is narrow: wrap long targets. inventory-count says what's selected.
            .props("dense flat wrap-cells hide-bottom")
            .classes("w-full")
            .mark("inventory")
        )
        # From the form, not "all": the selection must survive a re-render.
        table.selected = [r for r in rows if r["id"] in chosen]
        count = ui.label().classes("text-caption opacity-70").mark("inventory-count")

        def show_count() -> None:
            if table.selected or state.form.local or state.form.container or state.form.ssh:
                count.set_text(f"{len(table.selected)} of {len(rows)} selected")
            else:
                # build_targets' default: picking nothing runs on every host.
                count.set_text(f"none selected: Evaluate runs on all {len(rows)}")

        def on_select(_: Any) -> None:
            state.form.inventory_selected = sorted(int(r["id"]) for r in table.selected)
            show_count()
            refresh_targets()

        table.on_select(on_select)
        if host_filter is not None:

            def on_filter(e: ValueChangeEventArguments) -> None:
                # QTable's own filter: a small static table, on its visible columns.
                table.props["filter"] = e.value or ""
                table.update()

            host_filter.on_value_change(on_filter)
        nonlocal show_inventory_count
        show_inventory_count = show_count
        show_count()

    def discovery_path() -> Path:
        return inventory.path or _inventory_path or user_inventory_path()

    def discoverable() -> bool:
        """The inventory can take auto-discovery, and it hasn't been tried on it yet."""
        return inventory.empty and discovery_path() not in _discovery_tried

    async def discover_now() -> Path:
        """Auto-discovery into the inventory, for a run with no client target configured."""
        nonlocal inventory
        path = discovery_path()
        activity.push(f"no client targets configured: running auto-discovery into {path}")
        progress_label.set_text("running auto-discovery ...")
        render_inventory(discovering=True)
        try:
            inventory = load_inventory_targets(await asyncio.to_thread(_discover_into_once, path))
        except Exception as exc:
            logger.debug("auto-discovery failed", exc_info=True)
            inventory = InventoryLoad(path=path, targets=[], error=f"auto-discovery failed: {exc}")
        finally:
            progress_label.set_text("")
        state.form.inventory_selected = list(range(len(inventory.targets)))
        render_inventory()
        refresh_targets()
        return path

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
        state.inventory_ready = True
        render_inventory()
        # Collapsed is right when there's something to run; with nothing, the
        # panel is where to fix that.
        if refresh_targets().problem is not None:
            side_panel.show()

    # Started after the log handler is attached, so discovery's progress shows.
    inventory_task = asyncio.create_task(load_inventory())
    client.on_delete(inventory_task.cancel)

    reanalyze()


# Two tabs opened on a first run must not both run discovery; the second
# waits, then finds the file the first one wrote.
_discovery_lock = threading.Lock()


_discovery_tried: set[Path] = set()
"""Inventories auto-discovery already ran on in this process. It evaluates every
candidate and may pull images, so a fruitless one isn't repeated on every Evaluate."""


def _discover_inventory_once() -> Path | None:
    with _discovery_lock:
        _discovery_tried.add(user_inventory_path())
        return find_or_discover_inventory()


def _discover_into_once(path: Path) -> Path | None:
    with _discovery_lock:
        _discovery_tried.add(path)
        return discover_into(path)


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


WEBVIEW2_RUNTIMES = (
    "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}",  # stable
    "{2CD8A007-E189-409D-A2C8-9AF4EF3C72AA}",  # beta
    "{0D50BFEC-CD6A-4F9A-964C-C7416E3ACB10}",  # dev
    "{65C35B14-6C1D-4122-AC46-7148CC9D6497}",  # canary
)
"""EdgeUpdate client ids of the WebView2 runtimes pywebview looks for."""
WEBVIEW2_MIN = (86, 0, 622, 0)
NET_4_6_2 = 394802
"""The ``Release`` value of .NET Framework 4.6.2, which pywebview's WebView2 needs."""
WEBVIEW2_URL = "https://developer.microsoft.com/microsoft-edge/webview2/"


def read_registry(hive: str, key: str, name: str) -> object | None:
    """The registry value `hive`\\`key`\\`name`, or None if it's missing."""
    if sys.platform != "win32":
        return None
    import winreg

    try:
        with winreg.OpenKey(getattr(winreg, hive), key) as handle:
            return winreg.QueryValueEx(handle, name)[0]
    except OSError:
        return None


def _version(text: object) -> tuple[int, ...]:
    try:
        return tuple(int(part) for part in str(text).split("."))
    except ValueError:
        return ()


def windows_webview_problem() -> str | None:
    """Why pywebview would fall back to MSHTML (IE) on this Windows, or None.

    It uses Edge's WebView2 only when the runtime and .NET 4.6.2 are installed,
    and silently falls back to MSHTML, which can't load NiceGUI's ES modules.
    Windows Server 2019 ships without WebView2. Mirrors pywebview's own
    (private) check in ``webview/platforms/winforms.py``.
    """
    release = read_registry(
        "HKEY_LOCAL_MACHINE", r"SOFTWARE\Microsoft\NET Framework Setup\NDP\v4\Full", "Release"
    )
    if not isinstance(release, int) or release < NET_4_6_2:
        return "pywebview's Edge WebView2 needs .NET Framework 4.6.2 or later"
    # A 64-bit Windows keeps the machine-wide runtime in the 32-bit registry view.
    machine_root = "SOFTWARE" if machine() == "x86" else r"SOFTWARE\WOW6432Node"
    for runtime in WEBVIEW2_RUNTIMES:
        for hive, root in (("HKEY_CURRENT_USER", "SOFTWARE"), ("HKEY_LOCAL_MACHINE", machine_root)):
            key = rf"{root}\Microsoft\EdgeUpdate\Clients\{runtime}"
            if _version(read_registry(hive, key, "pv")) >= WEBVIEW2_MIN:
                return None
    return (
        "the Microsoft Edge WebView2 Runtime isn't installed, and pywebview's "
        f"fallback (IE) can't run this app; install it from {WEBVIEW2_URL} "
        "and meanwhile use Edge or Chrome"
    )


def native_window_problem() -> str | None:
    """Why a native window can't open here, or None if it can.

    pywebview ships with this package, but on Linux it brings no GUI toolkit
    of its own: it needs PyGObject (GTK) or Qt bindings, and a display. On
    Windows it needs Edge's WebView2 runtime.
    """
    if importlib.util.find_spec("webview") is None:
        return "pywebview isn't installed"
    if sys.platform == "win32":
        return windows_webview_problem()
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
        "favicon": LOGO,
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
        # The favicon doesn't reach the macOS Dock or the Windows taskbar, which
        # show Python's icon unless pywebview is given one in a format it reads.
        icon = WINDOW_ICONS.get(sys.platform, LOGO.with_suffix(".png"))
        nicegui_app.native.start_args["icon"] = str(icon)
    if args.port is not None:
        kwargs["port"] = args.port
    nicegui_app.on_shutdown(close_session_clients)
    ui.run(root, **kwargs)
