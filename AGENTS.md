# AGENTS.md

Guidance for AI coding agents working in this repo. Usage docs are in
[README.md](README.md).

## What this is

A NiceGUI (asyncio) browser app for BigFix client and session relevance. It
does live static analysis with `bigfix-relevance-analyzer`, evaluates client
relevance with real `qna` through `bigfix-remote-client-relevance` and
session relevance on every configured BigFix deployment through
`bigfix-remote-session-relevance`, and has a per-level breakdown (client
only).
Entry point: `bigfix_relevance_debugger.app:main`, run via
`uvx`/`uv run`.

- `src/bigfix_relevance_debugger/`
  - `analysis.py`: analyzer -> frozen view-model dataclasses, plus CodeMirror
    decoration specs (the breakdown highlight only). Pure and sync.
  - `evaluation.py`: target form -> `Target`s, inventory loading, streaming,
    session relevance (`stream_session`) and `run_breakdown`. Async.
  - `log_bridge.py`: library loggers -> UI callback, thread-safe.
  - `editor.py` + `relevance_codemirror.js`: the relevance editor, NiceGUI's
    CodeMirror wired to the analyzer's language server. See below.
  - `app.py`: the page and `main()`. UI wiring only.
  - `upstream/`: code meant to be donated to the libraries, one module per
    library (`remote_client_relevance.py`, `relevance_analyzer.py`). See
    below.
- `tests/unit/`: offline. No Docker, qna, SSH or network.

**Keep NiceGUI out of `analysis.py`, `evaluation.py` and `log_bridge.py`** so
they can be tested without a browser. Put logic there, not in `app.py`.

## Commands

```bash
uv sync
uv run pytest
uv run ruff check src tests
uv run ruff format src tests
uv run pyright src tests
scripts/ci_local.sh   # approximate CI locally (act + Docker) when GitHub Actions is down
```

`scripts/ci_local.sh --help` lists its jobs and how it differs from CI (no
Windows; Linux containers use the host's architecture). Its smoke steps copy
`test.yaml`'s `smoke` job, so change both together.

## How to work here

- **Test first.** Write the failing test, run it and confirm it fails for
  the right reason, then implement until green. A new test that passes
  before the change isn't testing the change. For example, NiceGUI's
  `should_see` also matches tooltip text.
- **Don't create or switch git branches** without asking.
- **No changes to the upstream libraries from here.** Use their public API:
  the package `__init__`, or a submodule's `__all__` (e.g.
  `bootstrap.targets.host_arch`, `discovery.run_auto_discovery`).
- **Prefer the library's implementation.** Before writing a helper, check
  whether a library already has it.
- **Write library-shaped code in `upstream/` so it can be donated.** If a
  helper belongs in a library but isn't there yet, it goes in that library's
  `upstream/` module. It imports only the stdlib and that library (enforced
  by `tests/unit/test_upstream_rules.py`) and uses that library's vocabulary.
  Its tests go in `tests/unit/upstream/` under the same rules. Translating
  between the two libraries (e.g. platform names) is app glue and stays out.
  Once a library ships the function, delete ours and call theirs.
- **Test evaluation with injected fakes.** `stream_results` and
  `run_breakdown` pass `**upstream` through to the library, so tests give a
  fake `transport_factory` and `resolver` and run the real orchestrator.
  `tests/conftest.py` redirects the library's compat memory to `tmp_path`.
- **Page tests** use `nicegui.testing.user_simulation(root=app.root)` and
  monkeypatch the names `app.py` imports (`stream_results`, `run_breakdown`,
  `find_inventory_path`, ...). Table cells aren't child elements, so read
  `table.rows` instead of using `should_see`. `user.find` only sees visible
  elements; a hidden one (e.g. the results section after Clear) has to be
  looked up in `user.client.elements`. The simulated user's
  `ui.download.content` treats a `str` as a URL, so patch `UserDownload`.
- **The side panel is a `ui.right_drawer`, closed by default.** A closed
  drawer is not hidden to the simulated user: `user.find` still finds what's
  inside, so assert `drawer.value` for open/closed. Because the panel's
  target count shows form problems (e.g. "Pick at least one target."),
  `should_see` of such a message passes before any click; check
  `user.notify` for warnings instead (the `notified` helper).
- **The header's target count is `evaluation.preview_targets`**, which builds
  the targets with `build_targets` and shares `waits_for_inventory` with
  `collect()`, so it can't drift from what Evaluate runs. Keep it that way.
- **The results table's row expansion is owned by Python** (its `expanded`
  prop plus `update:expanded`), so tests can read `table.props["expanded"]`.
  Sorting and filtering (`order_results`, `visible_results`) are pure
  functions in `evaluation.py`, and so is the answer summary (`AnswerTally`,
  one `add` per streamed result) and its pick (`ResultRow.has_key`).
- **Editor intelligence comes only from the analyzer's language server.**
  `editor.RelevanceEditor` extends `ui.codemirror`. Python owns the document:
  an in-process `bigfix_relevance_analyzer.lsp.StatementSession` (wrapping
  `lsp.Server`) is synced from the element's `value` and answers the
  browser's `lsp` events (`{id, method, params}`, only `ALLOWED_REQUESTS`,
  since the page is untrusted input) with `run_method("lspReply", ...)`;
  diagnostics go out with `run_method("lspNotify", ...)` after the debounce,
  carrying the `text` they were computed for. Its dialect follows
  `AnalysisView.lint_dialect`, so squiggles and the findings table agree.
  Don't add completion, hover or lint logic here; add it to the analyzer.
- **The browser half has no build step and one CodeMirror.**
  `relevance_codemirror.js` `extends` NiceGUI's own component (registered as
  the `nicegui-codemirror-base` ESM) and overrides `setupExtensions()`.
  The client and the highlighter come from the analyzer wheel's `web/`
  (`editor._ANALYZER_WEB`, served as the `bigfix-relevance-editor` ESM):
  `relevance_editor.js` (`relevanceEditor(CM, transport)`) and
  `relevance_language.js` (`relevanceLanguage(CM)`, generated from the VS
  Code grammar). Both import nothing and get CodeMirror passed in, so they
  only use what `nicegui-codemirror` exports, and the analyzer tests them
  against NiceGUI's CodeMirror. Nothing editor-related lives in `upstream/`;
  change them in the analyzer. `RelevanceEditor` refuses a `language`
  (NiceGUI would add a second one), and a failure to build the highlighter
  costs only the colors. `test_editor.py` pins the NiceGUI and analyzer
  files this relies on, including that NiceGUI's bundle exports each
  module's `REQUIRED_CM`. The browser half can't run in pytest:
  test the Python half (record `run_method`, `trigger("lsp", ...)`) and
  check the browser in a real run. The preview pane can report a 0x0
  viewport; resize it before testing hover. In a real run, never click
  Evaluate or Breakdown to test the editor: they run against the real
  inventory.
- **The inventory is shared** with the `bigfix-remote-client-relevance` CLI:
  by default it uses that library's `find_inventory_path()` search
  (`~/.bigfix/remote_clients.toml` etc.), and first-run auto-discovery when
  there is none. Don't invent a separate config file.
- **Dialect decides what Evaluate runs.** `analysis.summarize(text, mode)`
  sets `run_dialects` (session first) from `upstream.relevance_analyzer.dialect_fit`
  or the picker's override. Session results always sort first.
- **Session relevance connects with `bigfix-remote-session-relevance`'s own
  lookup** (`load_config`: its TOML file, else the `BES_*` env vars and
  `~/.besapi.conf`, plus a root server's own credentials). Don't add a
  config file of the app's own: the library's TOML file is shared with its
  CLI, as `~/.bigfix/remote_clients.toml` is for client relevance.
  `evaluation.SessionClients` holds the one process-wide
  `AsyncSessionRelevanceClient` (built lazily, rebuilt when the timeout
  changes or after a config failure, closed at shutdown). It runs on the
  event loop: no threads. `stream_session` yields one `SessionEvaluation`
  per deployment, or one `config` result when nothing is configured
  (`"config"` is this app's kind; the rest are the library's
  `ResultError.kind`). Tests build `SessionClients` over
  `default_session_client(opts, load=..., transport_factory=..., trust=None)`
  with fake servers (`httpx.MockTransport`), or monkeypatch
  `app.stream_session` and `app.expected_session_results`.
- **The session row count is `expected_session_results`**: the library's
  `result_count()` (one per deployment), else the connection count before
  deployment ids are discovered (the run corrects its progress total), and
  1 for the `config` row. The header's target count and progress both use
  it.
- **Tests must never connect to a real BigFix server.** `tests/conftest.py`
  points HOME, `BFRSR_CONFIG` and the `BES_*` vars at nothing, turns off the
  local root server lookup, makes `evaluation.default_session_client` fail
  loudly and resets the shared clients. Test modules import the real
  `default_session_client` before that patch, so they can still use it with
  a fake `load`.
- **Tests must never run real discovery.** It writes
  `~/.bigfix/remote_clients.toml`; `tests/conftest.py` makes it fail loudly.
- **Tests must never ask a real container engine.** The page looks up an
  `auto` arch in the background when Container is ticked;
  `tests/conftest.py` makes `evaluation.default_image_source` fail loudly, so
  page tests fake `app.resolve_container_arches`.
- **Smart defaults, no required clicks.** Infer the best option (inventory
  all selected, auto arch, the best breakdown target) and say why; selecting
  less is the override. Don't add dialogs that ask. Selecting nothing at all
  is not "run nothing": `evaluation.inventory_indexes` runs every inventory
  host. What an empty setup does depends on the dialect: client-only
  relevance with no host configured runs auto-discovery into the inventory
  first (`needs_discovery`, once per file per process; page tests fake
  `app.discover_into`); session-only relevance with no BigFix server shows
  the `config` row saying how to add one; both dialects run what is
  configured, never discover, and advise adding a server when there is none.
- **`ui.notify` and new elements need a slot, and NiceGUI keeps the slot
  stack per task.** Start page work in a task with `in_page(...)` (it enters
  the page's client), never a bare `asyncio.create_task`. The simulated
  user's `ui.notify` doesn't look the slot up, so `test_app.py`'s autouse
  `app_started` fixture makes it, as a served page does.
- **Logging, never print** (ruff `T20`). Library INFO goes to the activity
  log by default and DEBUG with `-v`, matching the library CLI's policy.
- **Check UI changes in a real run** when Docker is available, e.g. evaluate
  `name of operating system` on `debian:11` / `arm64` / qna `11.0.7.61`.
  For session relevance, `now` runs both dialects if a BigFix server is
  configured (read-only queries only).

## Releasing

Bump `[project] version` in `pyproject.toml` in a PR and merge it. That is
the only place the version lives (`__version__` reads the installed
metadata). On `main`, `.github/workflows/tag_and_release.yaml` sees an
untagged version, runs `test.yaml` and `pre-commit.yaml` as gates, then
tags `v<version>`, creates the GitHub release and publishes to PyPI with
trusted publishing. **Never tag by hand:** an existing tag is how the
workflow knows a version is already released, so a hand-made tag means that
version never reaches PyPI.

Until the repository variable `PYPI_PUBLISH` is `true`, the release job
(tag, GitHub release, PyPI) is skipped, but the version check and the test
and pre-commit gates still run, so Run workflow on `main` tests everything
up to the release without publishing.

A version with an `a`, `b`, `rc` or `.dev` part becomes a GitHub
pre-release, and the version must be in normalized PEP 440 form (`1.0.0b1`,
not `1.0.0-beta`) so the tag matches PyPI.

If a release fails partway, use **"Re-run failed jobs" on that run**. It
publishes the same bytes the gates tested, reuses the draft release, and
`uv publish --check-url` skips files PyPI already has. Run workflow on `main`
also works, but only if nothing that changes the package has merged since
the version bump, or if PyPI got nothing in the failed run. Otherwise PyPI
already has different bytes for that version and the run stops before
changing anything; bump the version instead.
