# bigfix-relevance-debugger

[![PyPI](https://img.shields.io/pypi/v/bigfix-relevance-debugger)](https://pypi.org/project/bigfix-relevance-debugger/)
[![test](https://github.com/jgstew/bigfix-relevance-debugger/actions/workflows/test.yaml/badge.svg)](https://github.com/jgstew/bigfix-relevance-debugger/actions/workflows/test.yaml)

A small browser GUI for BigFix **client and session relevance**:

1. **An editor backed by the analyzer's language server**, the same one the
   VS Code extension uses: syntax highlighting with the VS Code grammar's
   rules (keywords, operators, strings, numbers and comments), inspector
   completion while you type (or Ctrl-Space or Tab; Tab or Enter takes the
   selected suggestion), Enter (no suggestion list open) to **Evaluate**
   instead of a newline, hover descriptions of
   inspectors, casts and operators, lint findings underlined with a gutter
   marker, and quick fixes from the hover or Mod-. (Cmd-. on macOS),
   including "Replace `x` with `y`" for a misspelled inspector.
   **Live static analysis** alongside: parse errors with line and column,
   dialect, type check, one-click "did you mean" fixes and **Fix all safe
   issues**, complexity score, evaluation cost and costly inspectors. The
   findings' "did you mean" buttons stay beside the editor's quick fixes:
   they are visible without hovering and replace every use of the name at
   once, where a quick fix replaces the one you hovered. Uses
   [`bigfix-relevance-analyzer`](https://github.com/jgstew/bigfix-relevance-analyzer).
2. **Evaluation with real `qna`** on local, container (Docker/Podman) and
   SSH targets, with results streaming in as each target finishes. Uses
   [`bigfix-remote-client-relevance`](https://github.com/jgstew/bigfix-remote-client-relevance).
   **Session relevance** runs on every BigFix deployment you've configured,
   through its REST API, BigFix Explorer, Web Reports or BigFix SaaS, using
   [`bigfix-remote-session-relevance`](https://github.com/jgstew/bigfix-remote-session-relevance).
   The app works out which dialect the expression is in and runs client,
   session, or both.
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
uvx bigfix-relevance-debugger
```

The latest unreleased `main`:

```bash
uvx --from git+https://github.com/jgstew/bigfix-relevance-debugger bigfix-relevance-debugger
```

From a checkout:

```bash
uvx --from . bigfix-relevance-debugger
```

or `uv run bigfix-relevance-debugger`, or
`uv run python -m bigfix_relevance_debugger`.

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

## Client or session relevance

Evaluate looks at the expression and runs it as **client relevance** (qna on
the targets), **session relevance** (your BigFix servers), or **both**. Session
results are listed first. The **Dialect** picker shows what it decided and
why, e.g. `auto: only session relevance has bes computers`:

| The expression | Runs |
| --- | --- |
| uses client-only inspectors (`files`, `registry`, ...) | client |
| uses session-only inspectors (`bes computers`, ...) | session |
| is valid in either (`now`, `(1;2)`, string handling) | both |
| mixes the two (`bes computers` and `computer name`) | both, so each side's error shows |
| doesn't parse or type-check | whatever the text suggests (`bes ...` reads as session), otherwise client |

Pick **Client**, **Session** or **Both** in the picker to override it. A
session-only run doesn't need any targets selected. **Breakdown** runs client
relevance only for now.

### Connecting to your BigFix servers

Session relevance uses `bigfix-remote-session-relevance`'s own connection
lookup, shared with its CLI, so there's nothing to configure in this app:

1. `~/.bigfix/.remote_session_connections.toml` (or the file `BFRSR_CONFIG`
   names). It holds any number of connections, to the REST API, BigFix
   Explorer, Web Reports or BigFix SaaS; the easiest way to write it is

   ```bash
   uvx bigfix-remote-session-relevance --setup
   ```

2. only if that file doesn't exist: the `BES_ROOT_SERVER`, `BES_USER_NAME`
   and `BES_PASSWORD` environment variables, then a `[besapi]` section in
   `~/.besapi.conf`, as one REST API connection:

   ```ini
   [besapi]
   BES_ROOT_SERVER = https://<ROOT_SERVER>:52311
   BES_USER_NAME = <USER>
   BES_PASSWORD = <PASSWORD>
   ```

3. on a root server, that server's own REST API credentials are added as one
   more connection (`local-root-server`). Reading them needs root or
   Administrator. Opt out with `BFRSR_LOCAL_ROOT_SERVER=0`, or
   `local_root_server = false` under `[defaults]` in the TOML file.

Versions before 0.5.0 of this app also read `/etc/besapi.conf`,
`~/besapi.conf` and `./besapi.conf`; those aren't read any more. Run
`--setup`, or move the `[besapi]` section to `~/.besapi.conf`.

Every enabled connection runs, and each **deployment** gets one row, labeled
with the connection that answered. Connections that reach the same deployment
(say its REST API and its Explorer) count once: the library tries them in
order (Explorer, REST API, Web Reports) and fails over to the next if one
can't answer. The expanded row says which one did (`via rest`), with the
server's own evaluation time. Without a reachable Explorer, the REST API
reports that time in whole milliseconds, so it often reads `0ms`.

The app builds its connections on the first session run (or when the target
count first needs them), not at startup, and reuses them. If nothing is
configured, the client evaluation still runs. When auto added session
relevance only because the expression is valid in either dialect (`now`),
that is noted in the activity log, not shown as an error row. When you asked
for session relevance (a session-only expression, or **Session** / **Both**
in the picker), the session row says so and says what to set up. A config
you add while the app is running is picked up on the next run. Queries are
read-only (relevance queries through each interface's query API). Passwords are never prompted for; a connection without
one shows an `authentication` row.

TLS certificates are verified. A server with a certificate no CA vouches for
(the usual self-signed root server) is trusted on first use: the certificate
is pinned in `~/.bigfix/.remote_session_known_certs.toml` (shared with the
library's CLI, so a certificate you already trusted there is trusted here
too) and the activity log shows a WARNING with its SHA-256 fingerprint. Check
it against the server. If a pinned certificate later changes, that
connection shows a `tls` error and doesn't fail over; to accept the new
certificate, remove its entry from that file (or run the CLI's
`--forget-cert <host:port>`).

## Inventory (shared with `bigfix-remote-client-relevance`)

The side panel's **Inventory** table lists the hosts from the **same `remote_clients.toml` the
`bigfix-remote-client-relevance` CLI uses**, found with that library's own
search: `./remote_clients.toml`, then `~/.bigfix/remote_clients.toml`, then
the all-users config dir. Hosts added there, by hand or by its
`--auto-discovery`, show up here too. `--inventory PATH` points at a
different file. A file that fails to load is shown as an error in the panel
and doesn't stop the app.

**No inventory yet?** On first start, when none of those places has a file,
the app runs the library's **first-run auto-discovery**, as its CLI does. It
tries this machine's BigFix client, the developer.bigfix.com online
evaluator, and containers for each arch (images you have already pulled
first, else defaults), and **writes the hosts that work to
`~/.bigfix/remote_clients.toml`**. If none work, it writes a placeholder so
discovery doesn't repeat on every start. The page opens right away; the
table fills in when discovery finishes, and its progress is in the activity
log.

**Every inventory host is selected by default**, so **Evaluate** runs on the
whole inventory (plus any Local/Container/SSH target you tick under **Add
targets**). The inventory is a checkbox table with each host's kind,
platform and qna version(s); untick hosts to narrow a run, or use the header
checkbox for all or none. From 9 hosts it gets a filter box. With many
container hosts, the first run against images that aren't cached yet is
slow; **Max parallel** limits how many targets run at once.

### The side panel and the target count

Targets, options and the activity log live in a side panel that is
**collapsed by default**, so the editor and results get the full width. The
button at the right of the header opens and closes it, and **always shows how
many systems Evaluate would run on right now**, e.g. `12 systems`. Hover it
(or open the panel) for where they come from (`10 inventory · 2 container`),
and for how many runs that makes when hosts list several qna versions. It
reads:

- `≥ 1 system` for a Container with arch `auto` until the image's arches have
  been looked up. That happens in the background, once per image, and the
  count then turns exact (e.g. `2 systems` for `debian:11`).
- `finding inventory …` while first-run auto-discovery is still going.
- `12 systems + BigFix server` when session relevance runs too, and
  `BigFix server` when only session relevance runs; `2 BigFix servers` with
  two deployments configured.
- `0 systems`, with a warning icon, when nothing would run. The panel then
  opens itself and says why. It also opens when a run stops on a target or
  option problem, but it never closes itself.

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

1. `uvx bigfix-relevance-debugger`
2. Type `name of operating system`. Live analysis shows a clean parse, the
   type `singular string` and a low complexity score.
3. Open the side panel from the header (it reads e.g. `5 systems`), untick
   every **Inventory** host with the table's header checkbox, then tick
   **Container**, keep image `debian:11` and Arch `auto`, set qna
   version to `11.0.7.61`, and click **Evaluate**. `auto` runs every arch the
   image publishes, so the header count reads `2 systems` and the results
   table gets two rows, x86_64 and arm64,
   each with its answer count, answers, platform/arch, qna version and
   elapsed time. If the library had to step back to an older qna build for
   that image, the qna column says so ("fell back from ..."). Click a row's
   chevron to see every answer, the answer types and the raw qna output. The
   first run shows download and image-prep progress in the activity log.
4. Type `name of op`: completion offers `operating system` first; Tab or
   Enter takes it, and Enter again evaluates. Hover `operating system` for
   what it is and where it exists. Keywords, operators, strings and numbers
   are colored as you type. Type a typo, e.g. `name of operating sytem`.
   The text gets a wavy underline and a lint finding appears with
   suggestions. Hover the underline for the message and a **Replace** button per suggestion (Mod-.
   lists the same fixes), or click **operating system** in the findings to
   fix it. For a statement the
   analyzer can fix safely, e.g. `exists values of setting "x" of client`,
   hover `setting` and click the fix, press Mod-. there, or click
   **Fix all safe issues**; Ctrl-Z undoes any of them.
5. Type `names of files whose (size of it > 1000) of folder "/etc"` (or pick
   it from **Examples**) and click **Breakdown**. With several targets
   selected, it picks the best fit itself and says why. Per-level counts appear
   above any results, so the breakdown is in view whichever you ran last.
   The `whose` level shows filtered vs unfiltered counts, e.g. `7 of 43`.
   Hover a row to highlight that level in the editor.
6. Change the folder to one that doesn't exist (`"/nope"`) and break it down
   again. The first level that fails is highlighted with an arrow, and its
   error ("Singular expression refers to nonexistent object") is shown
   inline.
7. Session relevance (needs a BigFix server configured, see above): type
   `names of bes computers`. The picker says `auto: only session relevance
   has bes computers`. Click **Evaluate**: one row per deployment, each
   labeled with its connection, no targets needed. Then type `now`, which is valid in either dialect: the
   session row lists first, then the client rows.
8. Optional: put the inventory hosts back, add a second image
   (`debian:11, ubuntu:24.04`) or both arches, evaluate
   `names of files of folder "/etc"`, and watch the tables re-sort as results
   stream in. The **Answers** summary lists each file with how many systems
   have it. Click `lsb-release` there: only the systems with that file stay
   in the results; click it again to show them all. Or type `lsb-release` in
   the filter box: only the hosts with that answer stay, opened to show all
   their answers.
   **Cancel** stops a run and cleans up its containers. **Export JSON**
   saves every result, including any the filter hides. **Clear** empties the
   results and the breakdown (not the activity log).

**Before the demo:** run step 3 once so the qna download and image prep are
already cached, and have Docker running.

### Reading the results

The results area reads top to bottom: the **breakdown** (when you ran one),
then the **Answers** summary, then **Results by system**.

**Answers** has one row per distinct answer, with how many systems returned
it and a bar for its share of the systems in the run, most common first.
Then come one `(no answers)` row and one row per error kind
(`error: transport`, ...); they always go last. A system counts once per
answer, however many qna versions it ran, so plural answers' shares can add
up to more than 100%. It shows the 50 most common answers and says how many
more there are. It covers the whole run, whatever the filter hides, and
leaves the session results out (those are servers, not a fleet). With a
single system it's hidden, since it would only repeat that system's row.

**Click a row in Answers** to show only the systems it counted. They keep all
their rows, and a removable `Only: ...` chip says what's picked. Click the row
again, or the chip's ✕, to show everything. It combines with the filter box,
stays across Evaluate like the filter, and **Clear** drops it.

The results table has one row per result, best first: the session results
(marked `session`, one per deployment) at the top, then hosts with the most answers, then hosts that answered nothing, then errors (in red).
A host listing several qna versions in the inventory gets one row per
version, kept together and told apart by the qna column. The line above the
table counts them, e.g. `5 results: 3 with answers, 1 empty, 1 error`.
Columns sort on click.

The **filter box** searches the answers and error messages, ignoring case
(not host names, platforms or qna versions, so `11` doesn't match every
`debian:11` row). Hosts with no matching result are hidden. A host that does
match keeps *all* its rows, opened to show every answer, so you see each
matching computer's whole answer set. Clearing the filter restores the rows
and what you had opened.

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
uv run ruff format src tests
uv run pyright src tests
```

CI runs the same (with `ruff format --check`) on Python 3.11 and 3.13, plus pre-commit and a smoke test
of the built wheel through `uvx` on Linux, macOS and Windows.

Unit tests need no Docker, qna, network or BigFix server: evaluation runs
the real orchestrator against an injected fake transport, session relevance
runs the real library against fake servers (`httpx.MockTransport`) answering
with responses recorded from a real one, and the page is tested with NiceGUI's simulated user. See [AGENTS.md](AGENTS.md) for layout and
conventions.
