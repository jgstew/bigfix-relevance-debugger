# Mobile spike: iOS (and macOS) proofs of concept — issue #33

Three ways to put `bigfix-relevance-debugger`'s core on iOS, built far enough
to compare on a real simulator: **Python 3.15's official iOS support
(SwiftUI host)**, **BeeWare (Toga + Briefcase)** and **Kivy**. All three use
the same UI-free core, so the differences below are about the toolkits, not
the BigFix code.

Spike code: nothing here is wired into the shipped app, its tests or its
package. Built on 2026-10-29 with Xcode 16.2 / iOS 18.3 simulator on macOS
14.8 (Apple Silicon).

## TL;DR

| | Pure Python 3.15 + SwiftUI | BeeWare (Toga) | Kivy |
|---|---|---|---|
| Runs on iOS simulator | **Yes**, smoke 7/7 | **Yes**, smoke 7/7 | **Yes**, smoke 7/7 |
| Python on iOS | **3.15.2, python.org official framework** | **3.15.2, python.org official framework** (Briefcase fetches it) | 3.11.6, compiled by kivy-ios |
| Runs on macOS | Only as "Designed for iPad" (needs a signing team) or a separate macOS target | **Yes**, native Cocoa app (packaged `.app` built, 3.15.2) | Yes, desktop window (Python ≤ 3.13: no Kivy wheels for 3.14+) |
| Looks/feels native | **Most**: SwiftUI `Form`, `List`, tab bar, alerts, iPad top tabs | **Native widgets** (UIKit/AppKit), coarser layout control; iPadOS 18 tab bar overlaps content | No: custom-drawn (OpenGL) widgets; ignores the safe area (Dynamic Island covers a tab) |
| Editor highlighting | Yes: `UITextView` + analyzer spans | **Yes**: the native text view's `NSTextStorage` + analyzer spans | Yes: `CodeInput` + Pygments lexer driven by the analyzer |
| Lint squiggles in editor | Yes (dotted underline) | Yes (underline) | No (`CodeInput` can only colour) |
| Android later | UI rewrite (Kotlin/Compose); core reusable via Chaquopy | **Same code** (`briefcase … android`, Chaquopy) | Same code (Buildozer / python-for-android) |
| UI language | Swift (+ ~60 lines of C glue) | Python | Python + KV |
| Python start (sim, incl. core import) | 0.55 s ([About](docs/screenshots/swiftui-iphone-about.png)) | similar (same framework; not timed) | — |
| Debug `.app` size (sim) | 283 MB (no stdlib pruning yet) | 108 MB | 66 MB (kivy-ios prunes the stdlib) |

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
