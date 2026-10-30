"""BigFix Relevance: the Kivy proof of concept for issue #33.

Same features as the Toga app (editor with highlighting, live analysis,
Evaluate, connections with Keychain passwords, TOFU prompt), drawn by Kivy.
All BigFix logic is in ``bfrd_mobile_core``. Runs on Kivy's asyncio loop, so
the core's async API is used directly.

Desktop:  python main.py      (Kivy 2.3.1 has wheels up to Python 3.13)
iOS:      see build_ios.sh    (kivy-ios builds its own Python 3.11)
"""

from __future__ import annotations

import asyncio
import logging
import os

from bfrd_mobile_core import analysis, versions
from bfrd_mobile_core.connections import BACKENDS, Connection, ConnectionStore
from bfrd_mobile_core.evaluate import Evaluator, ResultRow
from bfrd_mobile_core.secrets import default_secret_store
from kivy.app import App
from kivy.clock import Clock
from kivy.core.window import Window
from kivy.lang import Builder
from kivy.properties import ListProperty, StringProperty
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.button import Button
from kivy.uix.label import Label
from kivy.uix.popup import Popup
from pygments.lexer import Lexer
from pygments.token import Comment, Error, Keyword, Name, Number, Punctuation, String, Text

log = logging.getLogger("bfrd_kivy")

EXAMPLE = "names of bes computers whose (now - last report time of it < 1 * day)"
DIALECTS = {
    "Auto": None,
    "Session": ("session",),
    "Client (Fast Query)": ("client",),
    "Both": ("session", "client"),
}
_TOKENS = {
    "keyword": Keyword,
    "string": String,
    "number": Number,
    "comment": Comment,
    "punct": Punctuation,
    "error": Error,
}


class RelevanceLexer(Lexer):
    """Pygments lexer driven by the analyzer's own tokenizer (no regex grammar)."""

    name = "BigFix Relevance"
    aliases = ("relevance",)

    def get_tokens_unprocessed(self, text):
        at = 0
        for span in analysis.highlight(text):
            if span.start > at:
                yield at, Name, text[at : span.start]
            yield span.start, _TOKENS.get(span.kind, Text), text[span.start : span.end]
            at = span.end
        if at < len(text):
            yield at, Name, text[at:]


KV = """
#:import RelevanceLexer __main__.RelevanceLexer
<Field@TextInput>:
    multiline: False
    size_hint_y: None
    height: dp(40)
    write_tab: False

<Root>:
    orientation: "vertical"
    TabbedPanel:
        do_default_tab: False
        tab_width: self.width / 3
        TabbedPanelItem:
            text: "Relevance"
            BoxLayout:
                orientation: "vertical"
                padding: dp(8)
                spacing: dp(6)
                CodeInput:
                    id: editor
                    lexer: RelevanceLexer()
                    style_name: "monokai"
                    font_name: "RobotoMono-Regular"
                    font_size: sp(15)
                    size_hint_y: 0.4
                    on_text: root.text_changed()
                Label:
                    id: status
                    text: ""
                    size_hint_y: None
                    height: self.texture_size[1] + dp(8)
                    text_size: self.width, None
                    color: 0.8, 0.8, 0.8, 1
                BoxLayout:
                    size_hint_y: None
                    height: dp(44)
                    spacing: dp(6)
                    Spinner:
                        id: dialect
                        text: "Auto"
                        values: list(root.dialect_names)
                    Field:
                        id: totp
                        hint_text: "TOTP (only if asked)"
                        input_filter: "int"
                        height: dp(44)
                    Button:
                        id: go
                        text: "Evaluate"
                        on_release: root.evaluate()
                ScrollView:
                    Label:
                        id: results
                        markup: True
                        text: root.results_markup
                        size_hint_y: None
                        height: self.texture_size[1]
                        text_size: self.width, None
                        valign: "top"
                        padding: dp(4), dp(4)
        TabbedPanelItem:
            text: "Connections"
            ScrollView:
                BoxLayout:
                    orientation: "vertical"
                    padding: dp(8)
                    spacing: dp(6)
                    size_hint_y: None
                    height: self.minimum_height
                    Spinner:
                        id: c_backend
                        text: "BigFix SaaS"
                        values: root.backend_names
                        size_hint_y: None
                        height: dp(44)
                    Field:
                        id: c_name
                        hint_text: "Name (e.g. saas, lab)"
                    Field:
                        id: c_server
                        text: "login.bigfix.com"
                        hint_text: "Server"
                    Field:
                        id: c_user
                        hint_text: "Username / email"
                    Field:
                        id: c_password
                        hint_text: "Password (kept in the Keychain)"
                        password: True
                    Field:
                        id: c_sid
                        hint_text: "SaaS server id (optional)"
                    Field:
                        id: c_computers
                        hint_text: "Fast Query computers: ids, or bes computers whose (...)"
                    Button:
                        text: "Save"
                        size_hint_y: None
                        height: dp(44)
                        on_release: root.save_connection()
                    Label:
                        text: root.saved_markup
                        markup: True
                        size_hint_y: None
                        height: self.texture_size[1] + dp(8)
                        text_size: self.width, None
        TabbedPanelItem:
            text: "About"
            Label:
                text: root.about
                text_size: self.width - dp(16), None
                halign: "left"
"""


class Root(BoxLayout):
    results_markup = StringProperty("")
    saved_markup = StringProperty("")
    about = StringProperty("")
    dialect_names = ListProperty(list(DIALECTS))
    backend_names = ListProperty(list(BACKENDS.values()))

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.store = ConnectionStore()
        self.secrets = default_secret_store()
        self.evaluator = Evaluator(self.secrets, trust_prompt=self.confirm_cert)
        self._analysis_ev = None
        self.about = "\n".join(f"{k}: {v}" for k, v in versions().items()) + (
            f"\nsecrets: {self.secrets.name}\ndata: {self.store.path.parent}"
        )
        self.refresh_connections()
        Clock.schedule_once(lambda _dt: setattr(self.ids.editor, "text", EXAMPLE))

    # -- editor ----------------------------------------------------------------

    def text_changed(self) -> None:
        if self._analysis_ev is not None:
            self._analysis_ev.cancel()
        self._analysis_ev = Clock.schedule_once(lambda _dt: self.analyze(), 0.25)

    def analyze(self) -> None:
        result = analysis.analyze(self.ids.editor.text)
        lines = [result.summary] + [f"{f.severity}: {f.message}" for f in result.findings[:3]]
        self.ids.status.text = "\n".join(lines)

    # -- evaluate --------------------------------------------------------------

    def add_row(self, row: ResultRow) -> None:
        def esc(s: str) -> str:
            return s.replace("&", "&amp;").replace("[", "&bl;").replace("]", "&br;")

        ms = f"{row.elapsed_ms:.0f} ms" if row.elapsed_ms is not None else ""
        meta = " · ".join(
            b for b in (row.dialect, row.connection, row.target, row.type or "", ms) if b
        )
        body = (
            "\n".join(esc(a) for a in row.answers[:50])
            if row.ok
            else f"[color=ff8a65]{esc(row.error or '')}[/color]"
        )
        self.results_markup += f"[b]{esc(meta)}[/b]\n{body or '(no answers)'}\n\n"

    def evaluate(self) -> None:
        asyncio.get_event_loop().create_task(self._evaluate())

    async def _evaluate(self) -> None:
        text = self.ids.editor.text.strip()
        if not text:
            return
        dialects = DIALECTS[self.ids.dialect.text] or analysis.analyze(text).dialects
        self.evaluator.totp_code = self.ids.totp.text.strip() or None
        self.ids.totp.text = ""
        self.results_markup = ""
        self.ids.go.disabled = True
        try:
            async for row in self.evaluator.run(text, dialects, self.store.load()):
                self.add_row(row)
        except Exception as exc:
            log.exception("evaluate failed")
            self.results_markup += f"[color=ff8a65]{exc}[/color]\n"
        finally:
            self.ids.go.disabled = False

    async def confirm_cert(self, info) -> bool:
        answer: asyncio.Future[bool] = asyncio.get_event_loop().create_future()
        box = BoxLayout(orientation="vertical", spacing=8, padding=8)
        box.add_widget(
            Label(
                text=f"{info.host_port} presented an untrusted certificate.\n\nSHA-256:\n"
                f"{info.fingerprint}\n\nTrust only if it matches the server's real "
                "certificate. It will be pinned.",
                text_size=(Window.width * 0.8, None),
                halign="center",
            )
        )
        buttons = BoxLayout(size_hint_y=None, height=48, spacing=8)
        popup = Popup(
            title="Trust this certificate?", content=box, size_hint=(0.9, 0.6), auto_dismiss=False
        )
        for label, value in (("Don't trust", False), ("Trust", True)):
            b = Button(text=label)
            b.bind(on_release=lambda _b, v=value: (popup.dismiss(), answer.set_result(v)))
            buttons.add_widget(b)
        box.add_widget(buttons)
        popup.open()
        return await answer

    # -- connections -------------------------------------------------------------

    def refresh_connections(self) -> None:
        self.saved_markup = (
            "\n".join(
                f"[b]{c.name}[/b]  {BACKENDS.get(c.backend, c.backend)} · {c.server} · {c.username}"
                + (" · key" if self.secrets.get(c.secret_account) else "")
                for c in self.store.load()
            )
            or "No connections yet."
        )

    def save_connection(self) -> None:
        ids = self.ids
        backend = next(k for k, v in BACKENDS.items() if v == ids.c_backend.text)
        conn = Connection(
            name=ids.c_name.text.strip() or backend,
            backend=backend,
            server=ids.c_server.text.strip(),
            username=ids.c_user.text.strip(),
            sid=ids.c_sid.text.strip() or None,
            computers=ids.c_computers.text.strip(),
        )
        self.store.upsert(conn)
        if ids.c_password.text:
            self.secrets.set(conn.secret_account, ids.c_password.text)
            ids.c_password.text = ""
        self.refresh_connections()

    # -- spike aids --------------------------------------------------------------

    async def smoke(self) -> None:
        from bfrd_mobile_core import smoke

        def report(line: str) -> None:
            log.info("%s", line)
            self.results_markup += line.replace("[", "&bl;").replace("]", "&br;") + "\n"

        await smoke.run(report)


class BigFixRelevanceApp(App):
    title = "BigFix Relevance"

    def build(self) -> Root:
        Builder.load_string(KV)
        root = Root()
        if os.environ.get("BFRD_SMOKE"):
            Clock.schedule_once(lambda _dt: asyncio.get_event_loop().create_task(root.smoke()), 1)
        if shot := os.environ.get("BFRD_SCREENSHOT"):
            Clock.schedule_once(lambda _dt: Window.screenshot(name=shot), 6)
        return root


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(BigFixRelevanceApp().async_run(async_lib="asyncio"))
