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
- **No changes to the upstream libraries.** Everything used is their public
  API, re-exported from each package's `__init__`.
- **Test evaluation with injected fakes.** `stream_results` and
  `run_breakdown` pass `**upstream` through to the library, so tests give a
  fake `transport_factory` and `resolver` and run the real orchestrator.
  `tests/conftest.py` redirects the library's compat memory to `tmp_path`.
- **Page tests** use `nicegui.testing.user_simulation(root=app.root)` and
  monkeypatch the names `app.py` imports (`stream_results`, `run_breakdown`,
  `find_inventory_path`, ...). Table cells aren't child elements, so read
  `table.rows` instead of using `should_see`.
- **The inventory is shared** with the `bigfix-remote-client-relevance` CLI:
  by default it uses that library's `find_inventory_path()` search
  (`~/.bigfix/remote_clients.toml` etc.). Don't invent a separate config file.
- **Logging, never print** (ruff `T20`). Library INFO goes to the activity
  log by default and DEBUG with `-v`, matching the library CLI's policy.
- **Check UI changes in a real run** when Docker is available, e.g. evaluate
  `name of operating system` on `debian:11` / `arm64` / qna `11.0.7.61`.
