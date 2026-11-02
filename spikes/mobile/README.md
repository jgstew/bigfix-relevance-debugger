# Mobile spike: iOS (and macOS) proofs of concept — issue #33

Four ways to put `bigfix-relevance-debugger` on iOS, built far enough to
compare on a real simulator:

- **Python 3.15's official iOS support** (SwiftUI host)
- **BeeWare** (Toga + Briefcase)
- **Kivy**
- **The existing NiceGUI app, as is**, in a `WKWebView` (added later; see
  [below](#nicegui-as-is-nicegui))

The first three use the same UI-free core, so the differences between them
are about the toolkits, not the BigFix code. The fourth runs the shipped
desktop app's code, unchanged.

Spike code: nothing here is wired into the shipped app, its tests or its
package. Built on 2026-10-29 with Xcode 16.2 / iOS 18.3 simulator on macOS
14.8 (Apple Silicon).

## TL;DR

| | Pure Python 3.15 + SwiftUI | BeeWare (Toga) | Kivy | NiceGUI as is |
|---|---|---|---|---|
| Runs on iOS simulator | **Yes**, smoke 7/7 | **Yes**, smoke 7/7 | **Yes**, smoke 7/7 | **Yes**: the real debugger page, analyzer + LSP live |
| Python on iOS | **3.15.2, python.org official framework** | **3.15.2, python.org official framework** (Briefcase fetches it) | 3.11.6, compiled by kivy-ios | **3.15.2, python.org official framework** |
| Runs on macOS | Only as "Designed for iPad" (needs a signing team) or a separate macOS target | **Yes**, native Cocoa app (packaged `.app` built, 3.15.2) | Yes, desktop window (Python ≤ 3.13: no Kivy wheels for 3.14+) | It already does (`uvx`) |
| Looks/feels native | **Most**: SwiftUI `Form`, `List`, tab bar, alerts, iPad top tabs | **Native widgets** (UIKit/AppKit), coarser layout control; iPadOS 18 tab bar overlaps content | No: custom-drawn (OpenGL) widgets; ignores the safe area (Dynamic Island covers a tab) | No: a web page (Quasar/Material); good on iPad, cramped on a phone |
| Editor highlighting | Yes: `UITextView` + analyzer spans | **Yes**: the native text view's `NSTextStorage` + analyzer spans | Yes: `CodeInput` + Pygments lexer driven by the analyzer | **Yes, plus completion and hover**: the app's CodeMirror + the analyzer's language server |
| Lint squiggles in editor | Yes (dotted underline) | Yes (underline) | No (`CodeInput` can only colour) | Yes (gutter + findings table) |
| Android later | UI rewrite (Kotlin/Compose); core reusable via Chaquopy | **Same code** (`briefcase … android`, Chaquopy) | Same code (Buildozer / python-for-android) | Same Python code; a small Android WebView host, and pydantic-core built for Android too |
| UI language | Swift (+ ~60 lines of C glue) | Python | Python + KV | **The existing Python/NiceGUI code** (+ ~90 lines of Swift) |
| Python start (sim, incl. core import) | 0.55 s ([About](docs/screenshots/swiftui-iphone-about.png)) | similar (same framework; not timed) | — | 2.9–3.2 s until the page answers |
| Debug `.app` size (sim) | 283 MB (no stdlib pruning yet) | 108 MB | 66 MB (kivy-ios prunes the stdlib) | 337 MB (no pruning; 175 MB is CPython's `test/`) |
| Compiled deps to build yourself | none | none | Python, SDL2, Kivy (kivy-ios) | **`pydantic-core` (Rust)**: nobody publishes iOS wheels |

**Recommendation: BeeWare/Toga**, with a SwiftUI shell as the fallback if the
UI ever needs more than Toga offers.

- Toga is the only option that gives native widgets, the official CPython
  3.15 iOS build, a native macOS app and an Android path from **one Python
  codebase**.
- The issue picked Kivy mainly for `CodeInput` highlighting. That reason no
  longer holds: Toga's text input *is* `UITextView`/`NSTextView`, and
  colouring its text storage from the analyzer's tokens gives highlighting,
  plus lint underlines, while keeping native selection, keyboard, dictation
  and autocorrect.
- SwiftUI feels the most "iOS/macOS native". The cost is two languages, and
  a second UI to write for Android.

> **Later decision (after this spike):** the chosen direction is SwiftUI over
> Python 3.15, sharing one headless core with the NiceGUI app; see
> jgstew/bigfix-relevance-debugger#41 and the upstream issues it lists. The
> NiceGUI-as-is spike below was added afterwards to answer "could the existing
> app just run on iOS?"

## What each PoC does

Same feature set in each, kept small:

- **Relevance tab:** editor with syntax colours, plus live analysis
  (dialect, type, parse error, lint findings with "did you mean"), debounced
  250 ms. A dialect picker (Auto / Session / Client (Fast Query) / Both), a
  TOTP field, Evaluate, and the results list.
- **Connections tab:** BigFix SaaS (default `login.bigfix.com`), Root REST,
  Explorer, Web Reports. The password goes to the **Keychain**; the
  connection list (no secrets) goes to a JSON file in the app's sandbox. Each
  connection also holds its Fast Query targets: computer ids, or `bes
  computers whose (…)`.
- **About tab:** versions and a self-check.
- **Trust on first use:** an untrusted certificate is never auto-accepted.
  Toga and Kivy ask in-flow (an async dialog). SwiftUI asks after the failed
  run, pins, and re-runs.
- **`BFRD_SMOKE=1`:** a shared self-check (`bfrd_mobile_core.smoke`) that
  needs no credentials. It covers analyzer, app data dir, Keychain
  round-trip, TLS + certifi (a handshake and an anonymous HEAD to
  `login.bigfix.com`), Evaluate with no connections, and the Fast Query
  import.

### Verified

| Check | SwiftUI iOS | Toga iOS | Kivy iOS | Toga macOS (packaged) | Kivy macOS | Core, macOS (3.15 and 3.11) |
|---|---|---|---|---|---|---|
| Smoke 7/7 | ✅ 3.15.2 | ✅ 3.15.2 | ✅ 3.11.6 | ✅ 3.15.2 | ✅ 3.13 | ✅ |
| Keychain via `ctypes` | ✅ (simulator) | ✅ (simulator) | ✅ (simulator) | ✅ | ✅ | ✅ |
| Highlighting | ✅ | ✅ | ✅ | ✅ | ✅ | — |
| Real session relevance (`now`) | — | — | — | — | — | ✅ on-prem (Explorer) + SaaS, through `Evaluator` (`core/scripts/real_session_check.py`) |
| Real Fast Query | not run | not run | not run | not run | not run | not run (it submits to endpoints and shows in operator history; offline tests only) |

Screenshots: [docs/screenshots](docs/screenshots).

## Shared core (`core/`)

`bfrd_mobile_core` is pure Python, with no UI imports:

- `analysis.py`: dialect choice (trimmed copy of the desktop app's
  `dialect_fit`), type summary, lint findings, and **highlight spans from the
  analyzer's own `tokenize()`**. No hand-written lexer or grammar to keep in
  sync.
- `evaluate.py`: `Evaluator.session()` (an `AsyncSessionRelevanceClient` over
  a `Config` built in code) and `Evaluator.fastquery()`
  (`TransportFastQuery` over a `RestBackend`/`SaasBackend`). It never calls
  `load_config()`, never reads `BES_*` env vars and never does the local root
  server lookup.
- `secrets.py`: Keychain generic-password items via `ctypes` against
  Security.framework. The same code works on macOS and iOS under every
  toolkit, with no pyobjus, rubicon or Swift. On iOS items are
  `WhenUnlockedThisDeviceOnly`.
- `connections.py`: app-sandbox storage (`~/Library/Application Support/<id>`
  is inside the container on iOS).
- `bridge.py`: JSON-string facade for the Swift host.

```bash
cd spikes/mobile/core && uv run -p 3.15 pytest -q        # 14 offline tests
```

**The whole BigFix stack is pure Python on iOS** once
`bigfix-remote-client-relevance` is installed with `--no-deps`. Its own deps
(`docker`, `asyncssh` → `cryptography`, `zstandard`) are compiled, have no
iOS wheels, and aren't imported on the Fast Query path. Importing the package
needs only `requests`, `beautifulsoup4` and `platformdirs`. Every PoC
therefore installs an explicit pinned pure-Python closure with `--no-deps`.

## Pure Python 3.15 + SwiftUI (`swiftui/`)

"Python 3.15's iOS support" means python.org now publishes an official
`python-3.15.x-iOS-XCframework.tar.gz`: a framework (device + simulator
slices), the stdlib, `utils.sh` to install it into an app, and an Objective-C
testbed project. It does **not** include a UI toolkit, so this PoC writes the
UI in SwiftUI and embeds CPython as the engine:

- `Sources/PythonHost.c` (~90 lines): initialises CPython with CPython's own
  testbed sequence, adds `app/` and `app_packages/`, and releases the GIL.
  Each call takes the GIL and runs `bridge.<fn>(json) -> json`.
- `Sources/*.swift`: `TabView`, `Form`, `List`, `.alert` for TOFU, and a
  `UITextView` wrapper for highlighting. SwiftUI's `TextEditor` only takes an
  `AttributedString` from iOS 26.
- `prepare.sh` downloads the framework and `uv pip install --target
  app_packages --no-deps` the pure-Python closure. `project.yml` (XcodeGen)
  wires in the framework and the `install_python` build phase.

```bash
cd spikes/mobile/swiftui && ./prepare.sh && xcodegen generate
xcodebuild -scheme BigFixRelevance -destination 'platform=iOS Simulator,name=iPhone 16 Pro' -derivedDataPath build build
```

Findings:

- It works, and it's the most native-feeling result: a real SwiftUI `Form`
  for connections, `SecureField` with password autofill, `.oneTimeCode` for
  TOTP, and iPad's top tab bar for free.
- Two languages. Every UI feature is Swift, and Python is a JSON service
  behind it. That's a clean split, but the UI isn't Python any more.
- **macOS:** the "My Mac (Designed for iPad)" destination appears, but it
  needs a provisioning profile. A free Apple ID "Personal Team" in Xcode is
  enough to run locally. No such team exists on this Mac, so it wasn't run.
  A true macOS SwiftUI target would need a macOS Python framework: python.org
  ships iOS/Android artefacts, not a macOS embeddable one; BeeWare's
  `Python-Apple-support` has one.
- **Android:** the Python core carries over (Chaquopy or CPython's Android
  embeddable package), but the UI would be rewritten in Kotlin/Compose.
- Size: the bundle ships all of CPython's `test/` package (175 MB of its
  283 MB). Prune `test`, `idlelib`, `tkinter` and `ensurepip` before
  release.

## BeeWare: Toga + Briefcase (`toga/`)

```bash
cd spikes/mobile/toga
python briefcase_spike.py create iOS && python briefcase_spike.py run iOS -d "iPhone 16 Pro::iOS 18.3"
python briefcase_spike.py build macOS && open "build/bfrd_toga/macos/app/BigFix Relevance.app"
```

(Briefcase 0.4.7 on Python 3.15 in its own venv. `briefcase_spike.py` is
plain `briefcase` plus the Xcode-gate relaxation explained below.)

Findings:

- **Briefcase ≥ 0.4.6 builds iOS apps on python.org's official 3.15
  framework** (`Using support package
  https://www.python.org/ftp/python/3.15.2/python-3.15.2-iOS-XCframework.tar.gz`).
  macOS still uses BeeWare's own support package (`Python-3.15-macOS-support.b3`).
- Native widgets on both platforms: `OptionContainer` is a `UITabBarController`,
  `DetailedList` has swipe-to-delete, and dialogs are native alerts. Toga's
  asyncio loop is the UI loop, so the TOFU dialog is just `await
  window.dialog(QuestionDialog(...))` inside the certificate callback.
- **Highlighting** (`src/bfrd_toga/highlight.py`): reach `widget._impl.native`
  (iOS `UITextView`) or `native_text` (macOS `NSTextView`) through
  rubicon-objc and colour its `textStorage`. About 80 lines, platform-neutral
  (UIKit and AppKit share the attribute names). It uses a private attribute
  (`_impl`), so pin Toga, or propose a rich-text or code-editor API upstream.
- **iPad bug:** on iPadOS 18, Toga 0.5.7's `OptionContainer` becomes the
  new floating top tab bar, but the content isn't inset, so the tab bar
  covers the first line of the editor ([screenshot](docs/screenshots/toga-ipad.png)).
  SwiftUI's `TabView` handles this itself ([screenshot](docs/screenshots/swiftui-ipad.png)).
  Report it upstream, or work around it with top padding on iPad.
- Rough edges: layout control is coarser than SwiftUI's (Pack styles). Toga
  is pre-1.0 (0.5.7). `Window.as_image()` doesn't capture `OptionContainer`
  tab labels on macOS (cosmetic, screenshots only).
- `requirement_installer_args = ["--no-deps"]` with the full pinned closure
  in `pyproject.toml` is what keeps the compiled deps out.
- **Android:** the same `pyproject.toml` already has an `android` section.
  Not built in this spike.

## Kivy (`kivy/`)

```bash
cd spikes/mobile/kivy && PYTHONPATH=../core/src python main.py   # desktop, Python 3.11–3.13
./build_ios.sh                                                    # kivy-ios, simulator
```

Findings:

- **Highlighting** via `CodeInput` + a ~15-line Pygments `Lexer` that yields
  the analyzer's spans (`RelevanceLexer`), so no regex grammar to maintain.
  `CodeInput` can colour but not underline, so lint findings show only in the
  status text.
- **iOS build works**: `build_ios.sh` → smoke 7/7 on the simulator
  ([screenshot](docs/screenshots/kivy-iphone-smoke.png)). The toolchain's
  `toolchain create` project builds with Xcode 16.2 unchanged.
- **Safe area:** the app draws under the Dynamic Island, which hides the
  middle tab's label. Kivy apps have to pad for `safe_area` themselves.
- Everything is custom-drawn. Text selection, the keyboard accessory, the
  context menu, dictation and Dynamic Type don't behave like iOS. That was
  the issue's main open risk, and it's confirmed visually on desktop.
- **No Python 3.15:** Kivy 2.3.1 (Dec 2024) has wheels up to 3.13, and
  kivy-ios 2025.5.17 compiles **CPython 3.11.6** itself (its `python3`
  recipe). The official 3.15 framework doesn't help Kivy. The core runs on
  3.11 (smoke 7/7), so this is a version lag, not a blocker today.
- The kivy-ios toolchain compiles Python, OpenSSL, libffi, SDL2 and Kivy from
  source. The first build takes tens of minutes and needs
  autoconf/automake/pkg-config from Homebrew.
- Separate toolchains per platform: kivy-ios for iOS, Buildozer /
  python-for-android for Android.

## NiceGUI as is (`nicegui/`)

**Question:** can the existing NiceGUI app run on iOS unchanged?

**Answer: yes, with two workarounds, neither in this repo's code:**

1. build `pydantic-core` for iOS yourself;
2. stub out `_multiprocessing` before NiceGUI imports.

Then `bigfix_relevance_debugger.app.main(["--no-browser", "--port", N])` (the exact desktop entry point, from this repo's own wheel) serves the real page on `127.0.0.1` inside the app, and a `WKWebView` shows it. Verified on the iOS 18.3 simulator:

- The page answers **2.9–3.2 s** after launch (Python start, imports, uvicorn).
- The CodeMirror editor with the analyzer's highlighter and language server, the lint gutter, the findings table with "did you mean", and the dialect/type/complexity chips all work ([iPhone](docs/screenshots/nicegui-iphone-analysis.png), [iPad](docs/screenshots/nicegui-ipad.png)).
- First-run **auto-discovery ran by itself**, as on desktop:
  - local qna failed: it **downloaded `BESAgent-11.0.7.61-ubuntu24.arm64.deb` first**, then "ios does not support processes";
  - Docker failed;
  - it then **added the `web-eval-rhel` online-evaluator target**, so client relevance can be evaluated through the online evaluator even on iOS.

  Evaluate was **not** clicked (AGENTS.md: never in a real run).

```bash
cd spikes/mobile/nicegui
./prepare.sh                 # Python.xcframework, pydantic-core for iOS (Rust), app_packages/ incl. this repo's wheel
xcodegen generate
xcodebuild -scheme BigFixRelevanceNiceGUI -destination 'platform=iOS Simulator,name=iPhone 16 Pro' -derivedDataPath build build
xcrun simctl install booted build/Build/Products/Debug-iphonesimulator/BigFixRelevanceNiceGUI.app
SIMCTL_CHILD_BFRD_DEMO_TEXT='name of operating sytem' xcrun simctl launch booted com.github.jgstew.bigfix-relevance-nicegui
```

### What it took

| Blocker | What happens on iOS | Spike workaround | Real fix (upstream) |
|---|---|---|---|
| **`pydantic-core`** (FastAPI → pydantic v2) | No iOS wheel on PyPI or BeeWare's index; FastAPI can't import without it | `build_pydantic_core.sh`: rustup + `aarch64-apple-ios[-sim]` targets; `cargo build` with PyO3 cross env vars (`PYO3_CROSS_LIB_DIR` = the framework's `lib-arm64/python3.15`, `-L` to `libpython3.15.dylib` → `@rpath/Python.framework/Python`). maturin refuses (it doesn't treat macOS → iOS as cross). About 1 min per build. | pydantic publishes iOS wheels (cibuildwheel supports iOS; maturin/PyO3 cross support) |
| **`_multiprocessing`, `_posixshmem`** | CPython for iOS has no fork or subprocesses, so they're absent. NiceGUI imports `multiprocessing.forkserver` / `.synchronize` at import time (for `run.cpu_bound`'s process pool) | `nicegui_ios_host._stub_multiprocessing()`: stub modules whose semaphore raises `NotImplementedError`, which NiceGUI's `run.setup()` already handles ("Failed to initialize ProcessPoolExecutor") | NiceGUI skips the process pool when `_multiprocessing` is missing (iOS, Android, Pyodide) |
| `orjson` | no iOS wheel | left out; NiceGUI falls back to `json` | `orjson; sys_platform != "ios" and …` in NiceGUI's deps |
| `uvicorn[standard]` extras (`uvloop`, `httptools`, `watchfiles`, `pyyaml`) | no iOS wheels | left out; plain uvicorn uses asyncio + h11 | marker on the extra in NiceGUI's deps |
| NiceGUI's `.nicegui/` storage | written relative to the working directory, which is `/` (read-only) on iOS | `chdir` to Application Support + `NICEGUI_STORAGE_PATH` | none needed |
| http to 127.0.0.1 | App Transport Security | `NSAllowsLocalNetworking` in Info.plist | none needed |
| qna download during discovery | a wasted ~MB download per first run on a device that can't run it | none (harmless here) | bigfix-remote-client-relevance: skip the local/downloaded qna probe where `subprocess` isn't supported (`sys.platform in ("ios", "android")`) |

`aiohttp`, `multidict` and `markupsafe` **do** ship iOS-simulator wheels on PyPI, and all the BigFix libraries installed unchanged. The client library was installed `--no-deps`, as in the other spikes, until jgstew/bigfix-remote-client-relevance#83 lands.

### Trade-offs vs. the SwiftUI plan

- **Gain: zero UI rework.** Every feature the desktop app has works on iPad immediately, including CodeMirror completion and hover, Breakdown, Export JSON, and the inventory dialog. On iPad it looks like the desktop app, which is a fair fit.
- **Lose: native feel.** It's a Material/Quasar web page:
  - no native `Form`s, swipe actions, Dynamic Type, or `SecureField` with password autofill;
  - on a phone the desktop layout is cramped (the findings table is clipped);
  - text editing is WebKit's, which is better than Kivy's but not native.
- **Lose: a heavier, more fragile stack.**
  - a Rust extension you build yourself for each pydantic release (pydantic pins `pydantic-core` exactly, so a version bump means a rebuild);
  - stubs for missing stdlib modules;
  - a local HTTP server and WebSocket inside the app;
  - 337 MB debug bundle before pruning.
- **App lifecycle:** iOS suspends a backgrounded app. The NiceGUI page reconnects when the app comes back, but long evaluations stop while it's in the background, as they would in any app. Untested beyond launch.
- **App Store:** an app whose UI is a local web page is allowed, but review guideline 4.2 (minimum functionality, "just a website") is a risk to plan for. The Xcode 26 / signing constraints below apply equally.

**Where it fits:** a fast way to put the *full* debugger on an iPad, for internal use, TestFlight, or a stopgap while the SwiftUI app (#41) is built. Keep it as an option rather than the main path. It also costs little to keep working once the upstream fixes above land, because it runs the same code as `uvx bigfix-relevance-debugger`.

## Library findings (candidates for upstream issues)

Answers to the issue's open questions, from reading 1.3.0 / 1.9.0 and
running the spike:

1. **Programmatic config: yes.** `AsyncSessionRelevanceClient(Config(connections=(ConnectionConfig(...),)), trust=TrustStore(KnownCerts(path), callback=...))`
   needs no file and no env vars, and `KnownCerts(path)` puts pins in the app
   sandbox.
2. **GUI-driven TOFU: yes** (`TrustStore(callback=async fn)`). ⚠️ With
   `callback=None` the store **auto-accepts** first-use certificates (it logs
   a warning), so an app must always pass a callback. Consider making
   auto-accept opt-in.
3. **GUI-driven TOTP: only partly.** `SaasBackend`'s TOTP prompt is a
   *synchronous* `Callable[[str], str]` called inside the async sign-in, so a
   GUI can't show a dialog from it. The spike collects the code beforehand
   and uses it once. With no code, the prompt raises instead of returning
   `""`, so no failed login is sent (SaaS locks an account after 100). Ask
   upstream for an async prompt.
4. **Pin at handshake: likely yes, by reading the code.** A pinned host gets
   `pinned_context(pem)` as its `SSLContext`, so a mismatched certificate
   fails in the TLS handshake before any request. Not tested here; worth the
   test the issue asks for.
5. **Runs on iOS as-is: yes.** Session relevance's deps are all pure Python
   and ran on the simulator (TLS through certifi + the framework's OpenSSL).
6. **Fast Query is delivered** (client 1.9.0, session 1.3.0) and reuses the
   session library's REST/SaaS connections, credentials and pins.
   `bigfix-remote-client-relevance` would install cleanly on mobile if
   `docker`, `asyncssh` and `zstandard` moved to extras (e.g. `[container]`,
   `[ssh]`).
7. Minor: `bigfix_remote_session_relevance` has no `__version__` (the
   spike uses `importlib.metadata`).

**SSH from iOS (open):** `asyncssh` needs `cryptography`, which is compiled.
BeeWare publishes some iOS binary wheels, but I didn't check whether a
current `cryptography` is among them. qna would run on the remote host, so
SSH is plausible if those wheels exist.

## Platform constraints that apply to every option

- **App Store builds need Xcode 26** (Apple's rule since April 2026), and
  Xcode 26 needs macOS 15.6+. This Mac runs macOS 14.8 / Xcode 16.2.
  Briefcase 0.4.6+ enforces the rule even for simulator builds;
  `toga/briefcase_spike.py` lowers the floor to 16.0 **for this spike only**.
  Whatever toolkit wins, the build Mac needs upgrading before shipping.
- iOS deployment target: Python 3.15's framework supports iOS 13+. The
  SwiftUI PoC targets iOS 17 (for `ContentUnavailableView` and the new
  `onChange`). Requiring the newest iOS gains nothing except SwiftUI's
  `TextEditor(AttributedString)` on iOS 26.
- Running on a device, TestFlight or the App Store needs an Apple developer
  account and signing. Everything here ran unsigned in the simulator.

## Not done / next steps

- A real Fast Query run (needs your go-ahead: it targets live endpoints).
- A SaaS sign-in from the iOS simulator with real credentials, and TOTP.
- Android builds (Toga: `briefcase create android`).
- Stdlib pruning and release-size numbers; device builds.
