# AGENTS.md

Guidance for AI coding agents working in this repo. Usage docs are in
[README.md](README.md).

## What this is

A NiceGUI (asyncio) browser app for BigFix client relevance. It does live
static analysis with `bigfix-relevance-analyzer`, evaluates with real `qna`
through `bigfix-remote-client-relevance`, and has a per-level breakdown.
Entry point: `bigfix_relevance_debugger.app:main`, run via
`uvx`/`uv run`.

- `src/bigfix_relevance_debugger/`
  - `analysis.py`: analyzer -> frozen view-model dataclasses, plus CodeMirror
    decoration specs. Pure and sync.
  - `evaluation.py`: target form -> `Target`s, inventory loading, streaming,
    and `run_breakdown`. Async.
  - `log_bridge.py`: library logger -> UI callback, thread-safe.
  - `app.py`: the page and `main()`. UI wiring only.
  - `upstream/`: code meant to be donated to the libraries, one module per
    library (`remote_client_relevance.py`, `relevance_analyzer.py`). See below.
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
- **The results table's row expansion is owned by Python** (its `expanded`
  prop plus `update:expanded`), so tests can read `table.props["expanded"]`.
  Sorting and filtering (`order_results`, `visible_results`) are pure
  functions in `evaluation.py`.
- **The inventory is shared** with the `bigfix-remote-client-relevance` CLI:
  by default it uses that library's `find_inventory_path()` search
  (`~/.bigfix/remote_clients.toml` etc.), and first-run auto-discovery when
  there is none. Don't invent a separate config file.
- **Tests must never run real discovery.** It writes
  `~/.bigfix/remote_clients.toml`; `tests/conftest.py` makes it fail loudly.
- **Smart defaults, no required clicks.** Infer the best option (inventory
  all selected, auto arch, the best breakdown target) and say why; selecting
  less is the override. Don't add dialogs that ask.
- **Logging, never print** (ruff `T20`). Library INFO goes to the activity
  log by default and DEBUG with `-v`, matching the library CLI's policy.
- **Check UI changes in a real run** when Docker is available, e.g. evaluate
  `name of operating system` on `debian:11` / `arm64` / qna `11.0.7.61`.
