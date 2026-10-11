# AGENTS.md

Guidance for AI coding agents working in this repo. Usage docs are in
[README.md](README.md).

## What this is

A NiceGUI (asyncio) browser app for BigFix client and session relevance. It
does live static analysis with `bigfix-relevance-analyzer`, evaluates client
relevance with real `qna` through `bigfix-remote-client-relevance` and
session relevance on a root server through `besapi`, and has a per-level
breakdown (client only).
Entry point: `bigfix_relevance_debugger.app:main`, run via
`uvx`/`uv run`.

- `src/bigfix_relevance_debugger/`
  - `analysis.py`: analyzer -> frozen view-model dataclasses, plus CodeMirror
    decoration specs. Pure and sync.
  - `evaluation.py`: target form -> `Target`s, inventory loading, streaming,
    session relevance (`evaluate_session`) and `run_breakdown`. Async.
  - `log_bridge.py`: library logger -> UI callback, thread-safe.
  - `app.py`: the page and `main()`. UI wiring only.
  - `upstream/`: code meant to be donated to the libraries, one module per
    library (`remote_client_relevance.py`, `relevance_analyzer.py`,
    `besapi.py`). See below.
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
```

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
- **The inventory is shared** with the `bigfix-remote-client-relevance` CLI:
  by default it uses that library's `find_inventory_path()` search
  (`~/.bigfix/remote_clients.toml` etc.), and first-run auto-discovery when
  there is none. Don't invent a separate config file.
- **Dialect decides what Evaluate runs.** `analysis.summarize(text, mode)`
  sets `run_dialects` (session first) from `upstream.relevance_analyzer.dialect_fit`
  or the picker's override. Session results always sort first.
- **Session relevance connects with besapi's own lookup**
  (`plugin_utilities.get_besapi_connection(None)`: root server credentials,
  `BES_*` env vars, then `besapi.conf` files). Don't add another config file.
  Avoid `init_plugin` (it parses `sys.argv`, calls `basicConfig(force=True)`
  and exits) and `session_relevance_array` (it loses tuple answers); parse
  the `session_relevance_xml` response with `upstream.besapi`. Tests inject a
  `SessionConnector` with a fake connection (`evaluate_session(...,
  connector=...)`) or monkeypatch `app.evaluate_session`.
- **Tests must never connect to a real BigFix server.** `tests/conftest.py`
  makes besapi's connection fail loudly and resets the shared connector.
- **Tests must never run real discovery.** It writes
  `~/.bigfix/remote_clients.toml`; `tests/conftest.py` makes it fail loudly.
- **Tests must never ask a real container engine.** The page looks up an
  `auto` arch in the background when Container is ticked;
  `tests/conftest.py` makes `evaluation.default_image_source` fail loudly, so
  page tests fake `app.resolve_container_arches`.
- **Smart defaults, no required clicks.** Infer the best option (inventory
  all selected, auto arch, the best breakdown target) and say why; selecting
  less is the override. Don't add dialogs that ask.
- **Logging, never print** (ruff `T20`). Library INFO goes to the activity
  log by default and DEBUG with `-v`, matching the library CLI's policy.
- **Check UI changes in a real run** when Docker is available, e.g. evaluate
  `name of operating system` on `debian:11` / `arm64` / qna `11.0.7.61`.
  For session relevance, `now` runs both dialects if a root server is
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

The whole workflow is a no-op until the repository variable `PYPI_PUBLISH`
is `true`. If a release fails partway, re-run it ("Re-run failed jobs", or
Run workflow on `main`): it reuses the draft release, and `uv publish
--check-url` skips files PyPI already has.
