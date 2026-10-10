# bigfix-relevance-debugger

A small browser GUI for BigFix **client relevance**:

1. **Live static analysis** while you type: parse errors with line and
   column, dialect, type check, lint findings with "did you mean"
   suggestions, complexity score, evaluation cost and costly inspectors. Uses
   [`bigfix-relevance-analyzer`](https://github.com/jgstew/bigfix-relevance-analyzer).
2. **Evaluation with real `qna`** on local, container (Docker/Podman) and
   SSH targets, with results streaming in as each target finishes. Uses
   [`bigfix-remote-client-relevance`](https://github.com/jgstew/bigfix-remote-client-relevance).
3. A **per-level breakdown**, like the Fixlet Debugger's graphical breakdown.
   It runs one count probe per level of the expression on the chosen endpoint
   and shows how many objects each level produced, so you can see
   **where the relevance goes empty on that machine**.

It is a localhost tool: it binds to `127.0.0.1` by default and has no auth.

The **activity log is per process**, not per window: with `--browser` or
`--no-browser` and several tabs open, each tab's log shows the library's
progress for every tab's runs.

## Run

With [uv](https://docs.astral.sh/uv/) installed:

```bash
uvx --from git+https://github.com/jgstew/bigfix-relevance-debugger bigfix-relevance-debugger
```

From a checkout:

```bash
uvx --from . bigfix-relevance-debugger
```

or `uv run bigfix-relevance-debugger`, or
`uv run python -m bigfix_relevance_debugger`. Once it's published to
PyPI, plain `uvx bigfix-relevance-debugger` will work.

It opens in a **native app window** by default (via pywebview, which is
installed with it). Closing the window stops the app.

| Option | Meaning |
| --- | --- |
| `--browser` | open in a browser tab instead of a native window |
| `--no-browser` | open no window at all, just serve (browse to the printed URL) |
| `--host HOST` | interface to bind (default `127.0.0.1`) |
| `--port PORT` | port (default: a free port for the native window, 8080 for a browser tab) |
| `--inventory PATH` | `remote_clients.toml` to offer as targets (default: see below) |
| `-v`, `--verbose` | show library DEBUG logs in the activity log (default INFO) |

**Linux:** pywebview brings no GUI toolkit of its own there, so without
GTK (PyGObject) or Qt bindings, or without a display, the app says why and
opens a browser tab instead. For a native window, add a toolkit, e.g.
`uvx --with 'pywebview[qt]' bigfix-relevance-debugger`.

## Prerequisites

- **Containers:** a running Docker (or Podman) daemon. The first run against
  an image downloads qna and prepares the image, which takes a while; the
  activity log shows the progress. Later runs reuse the cache.
- **SSH:** key, agent or `~/.ssh/config` authentication only. There is no
  password support. *Become* uses `sudo -n`, which never prompts. Unknown
  host keys fail unless you untick *Verify host key*. To use a non-default
  key or port, set up a host alias in `~/.ssh/config` and enter the alias
  as the host.
- **Local:** whether a local qna is available depends on the platform (see
  the `bigfix-remote-client-relevance` docs). Containers are the reliable path
  on macOS.

## Inventory (shared with `bigfix-remote-client-relevance`)

The target picker lists the hosts from the **same `remote_clients.toml` the
`bigfix-remote-client-relevance` CLI uses**, found with that library's own
search: `./remote_clients.toml`, then `~/.bigfix/remote_clients.toml`, then
the all-users config dir. Hosts added there, by hand or by its
`--auto-discovery`, show up here too. `--inventory PATH` points at a
different file. A file that fails to load is shown as an error in the picker
and doesn't stop the app.

**No inventory yet?** On first start, when none of those places has a file,
the app runs the library's **first-run auto-discovery**, as its CLI does. It
tries this machine's BigFix client, the developer.bigfix.com online
evaluator, and containers for each arch (images you have already pulled
first, else defaults), and **writes the hosts that work to
`~/.bigfix/remote_clients.toml`**. If none work, it writes a placeholder so
discovery doesn't repeat on every start. The page opens right away; the
picker fills in when discovery finishes, and its progress is in the activity
log.

**Every inventory host is selected by default**, so **Evaluate** runs on the
whole inventory (plus any Local/Container/SSH target you tick). Remove hosts
from the picker to narrow a run. With many container hosts, the first run
against images that aren't cached yet is slow; **Max parallel** limits how
many targets run at once.

**Breakdown** runs on one endpoint and picks it for you from the selected
targets, without asking:

1. **Platform fit.** The analyzer reports which platforms the expression's
   inspectors exist on (e.g. `registry` is Windows-only, `rpm` is RHEL-family).
   A target's platform comes from its inventory `platform`, its container
   image name, or this machine for `local`. A known match wins, then unknown,
   then a known mismatch.
2. **Kind.** Containers, then SSH, local, fastquery, and the online evaluator
   last, since that one sends the relevance to an outside service.
3. **Native arch.** A container in this machine's arch (e.g. arm64 on Apple
   Silicon) beats an emulated one.
4. **Selection order.**

The breakdown header says which target was picked and why. To break down on
a specific target, select only that one.

## Demo script

1. `uvx --from git+https://github.com/jgstew/bigfix-relevance-debugger bigfix-relevance-debugger`
2. Type `name of operating system`. Live analysis shows a clean parse, the
   type `singular string` and a low complexity score.
3. Clear the **Inventory** picker (every host in it is selected by default),
   then tick **Container**, keep image `debian:11` and Arch `auto`, set qna
   version to `11.0.7.61`, and click **Evaluate**. `auto` runs every arch the
   image publishes, so two rows appear, x86_64 and arm64, each with its qna
   version, platform/arch and elapsed time. If the
   library had to step back to an older qna build for that image, the row
   says so ("fell back from ..."). The first run shows download and
   image-prep progress in the activity log.
4. Type a typo, e.g. `name of operating sytem`. The text gets a wavy
   underline and a lint finding appears with suggestions.
5. Type `names of files whose (size of it > 1000) of folder "/etc"` (or pick
   it from **Examples**) and click **Breakdown**. With several targets
   selected, it picks the best fit itself and says why. Per-level counts appear.
   The `whose` level shows filtered vs unfiltered counts, e.g. `7 of 43`.
   Hover a row to highlight that level in the editor.
6. Change the folder to one that doesn't exist (`"/nope"`) and break it down
   again. The first level that fails is highlighted with an arrow, and its
   error ("Singular expression refers to nonexistent object") is shown
   inline.
7. Optional: put the inventory hosts back, add a second image
   (`debian:11, ubuntu:24.04`) or both arches, then **Evaluate** to watch
   results stream in completion order.
   **Cancel** stops a run and cleans up its containers. **Export JSON**
   saves the results.

**Before the demo:** run step 3 once so the qna download and image prep are
already cached, and have Docker running.

### Reading the breakdown

| Objects | Outcome | Meaning |
| --- | --- | --- |
| `N` | `count` | the level produced N objects (summed over its contexts) |
| `0` | `empty-or-error` | produced nothing, *or* errored in a plural context, which relevance flattens to empty. The probe can't tell these apart. |
| | `not-evaluable` | `-1`: couldn't be evaluated, e.g. a singular reference to a missing object |
| | `no-context` | the probe answered nothing, because its context produced no objects |
| | `error` | qna or the transport reported an error for this probe (shown inline) |

Levels are listed whole expression first, then in the analyzer's order. The
**first failing level after the whole expression** is flagged, since that
answers "why is my relevance false?".

## Development

```bash
uv sync
uv run pytest
uv run ruff check src tests
uv run pyright src tests
```

Unit tests need no Docker, qna or network: evaluation runs the real
orchestrator against an injected fake transport, and the page is tested with
NiceGUI's simulated user. See [AGENTS.md](AGENTS.md) for layout and
conventions.
