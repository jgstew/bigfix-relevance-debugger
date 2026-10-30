"""BigFix Relevance: the Toga (BeeWare) proof of concept for issue #33.

Three tabs: Relevance (editor, live analysis, Evaluate, results),
Connections (SaaS / REST / Explorer / Web Reports, passwords in the
Keychain), About. All BigFix logic is in ``bfrd_mobile_core``.
"""

from __future__ import annotations

import asyncio
import logging
import os

import toga
from bfrd_mobile_core import analysis, versions
from bfrd_mobile_core.connections import BACKENDS, Connection, ConnectionStore
from bfrd_mobile_core.evaluate import Evaluator, ResultRow
from bfrd_mobile_core.secrets import default_secret_store
from toga.constants import COLUMN, ROW
from toga.fonts import MONOSPACE
from toga.style import Pack

from . import highlight

log = logging.getLogger(__name__)

DIALECT_CHOICES = {
    "Auto": None,
    "Session": ("session",),
    "Client (Fast Query)": ("client",),
    "Both": ("session", "client"),
}
EXAMPLE = "names of bes computers whose (now - last report time of it < 1 * day)"


def _row_item(row: ResultRow) -> dict[str, str]:
    if row.ok:
        answers = row.answers
        title = ", ".join(answers[:5]) + (f" … (+{len(answers) - 5})" if len(answers) > 5 else "")
        title = title or "(no answers)"
    else:
        title = f"⚠️ {row.error}"
    ms = f"{row.elapsed_ms:.0f} ms" if row.elapsed_ms is not None else ""
    bits = [row.dialect, row.connection, row.target, row.type or "", ms]
    return {"title": title, "subtitle": " · ".join(b for b in bits if b)}


class BigFixRelevance(toga.App):
    def startup(self) -> None:
        logging.basicConfig(level=logging.INFO)
        self.store = ConnectionStore()
        self.secrets = default_secret_store()
        self.evaluator = Evaluator(self.secrets, trust_prompt=self.confirm_cert)
        self._analysis_task: asyncio.Task | None = None
        self.current = analysis.analyze("")

        self.main_window = toga.MainWindow(title=self.formal_name)
        self.main_window.content = toga.OptionContainer(
            content=[
                ("Relevance", self.build_relevance()),
                ("Connections", self.build_connections()),
                ("About", self.build_about()),
            ],
            style=Pack(flex=1),
        )
        self.main_window.show()
        if tab := os.environ.get("BFRD_TAB"):  # spike aid for screenshots
            self.main_window.content.current_tab = ["relevance", "connections", "about"].index(tab)
        self.editor.value = EXAMPLE
        self.schedule_analysis(self.editor)
        if os.environ.get("BFRD_SMOKE"):
            asyncio.get_event_loop().create_task(self._smoke())
        if shot := os.environ.get("BFRD_SCREENSHOT"):
            asyncio.get_event_loop().create_task(self._screenshot(shot))

    async def _smoke(self) -> None:
        """Spike aid: run the shared self-check and list it under the editor."""
        from bfrd_mobile_core import smoke

        def report(line: str) -> None:
            log.info("%s", line)
            status, _, rest = line.partition(" ")
            self.results.data.append({"title": f"{status} {rest.split(':')[0]}", "subtitle": rest})

        await smoke.run(report)

    async def _screenshot(self, path: str) -> None:
        """Spike aid: save the window to ``path`` once analysis has run."""
        await asyncio.sleep(5)
        self.main_window.as_image().save(path)
        log.info("screenshot saved to %s", path)

    # -- Relevance tab -------------------------------------------------------

    def build_relevance(self) -> toga.Widget:
        self.editor = toga.MultilineTextInput(
            placeholder="Relevance…",
            on_change=self.schedule_analysis,
            style=Pack(flex=1, font_family=MONOSPACE, font_size=14, height=160),
        )
        self.status = toga.Label("", style=Pack(margin=(4, 8), font_size=12))
        self.findings = toga.Label("", style=Pack(margin=(0, 8), font_size=12, color="#b26a00"))
        self.dialect = toga.Selection(items=list(DIALECT_CHOICES), style=Pack(flex=1))
        self.totp = toga.TextInput(placeholder="TOTP (only if asked)", style=Pack(width=150))
        self.evaluate_button = toga.Button(
            "Evaluate", on_press=self.evaluate, style=Pack(margin_left=8)
        )
        self.spinner = toga.ActivityIndicator()
        self.results = toga.DetailedList(style=Pack(flex=1))
        controls = toga.Box(
            children=[self.dialect, self.totp, self.spinner, self.evaluate_button],
            style=Pack(direction=ROW, margin=8, align_items="center", gap=6),
        )
        return toga.Box(
            children=[self.editor, self.status, self.findings, controls, self.results],
            style=Pack(direction=COLUMN, flex=1),
        )

    def schedule_analysis(self, widget: toga.Widget) -> None:
        """Debounced: analyze 250 ms after typing stops."""
        if self._analysis_task is not None:
            self._analysis_task.cancel()
        self._analysis_task = asyncio.get_event_loop().create_task(self._analyze_later())

    async def _analyze_later(self) -> None:
        await asyncio.sleep(0.25)
        text = self.editor.value
        self.current = result = analysis.analyze(text)
        self.status.text = result.summary
        self.findings.text = "\n".join(f"{f.severity}: {f.message}" for f in result.findings[:3])
        highlight.apply(self.editor, text, result.spans)

    async def evaluate(self, widget: toga.Widget) -> None:
        text = self.editor.value.strip()
        if not text:
            return
        choice = DIALECT_CHOICES[self.dialect.value]
        dialects = choice or analysis.analyze(text).dialects
        self.evaluator.totp_code = self.totp.value.strip() or None
        self.totp.value = ""
        self.results.data = []
        self.evaluate_button.enabled = False
        self.spinner.start()
        try:
            async for row in self.evaluator.run(text, dialects, self.store.load()):
                self.results.data.append(_row_item(row))
        except Exception as exc:
            log.exception("evaluate failed")
            self.results.data.append({"title": f"⚠️ {exc}", "subtitle": type(exc).__name__})
        finally:
            self.spinner.stop()
            self.evaluate_button.enabled = True

    async def confirm_cert(self, info) -> bool:
        """Trust on first use, asked in-flow: Toga's loop *is* asyncio's."""
        return await self.main_window.dialog(
            toga.QuestionDialog(
                "Trust this certificate?",
                f"{info.host_port} presented a certificate that isn't trusted yet.\n\n"
                f"SHA-256:\n{info.fingerprint}\n\n"
                "Trust it only if this matches the server's real certificate. "
                "It will be pinned; a different one later is refused.",
            )
        )

    # -- Connections tab -----------------------------------------------------

    def build_connections(self) -> toga.Widget:
        self.c_backend = toga.Selection(
            items=list(BACKENDS.values()), on_change=self.backend_changed
        )
        self.c_name = toga.TextInput(placeholder="Name (e.g. saas, lab)")
        self.c_server = toga.TextInput(value="login.bigfix.com", placeholder="Server")
        self.c_user = toga.TextInput(placeholder="Username / email")
        self.c_password = toga.PasswordInput(placeholder="Password (kept in the Keychain)")
        self.c_sid = toga.TextInput(placeholder="SaaS server id (optional, bf…)")
        self.c_computers = toga.TextInput(
            placeholder="Fast Query computers: ids, or bes computers whose (…)"
        )
        save = toga.Button("Save", on_press=self.save_connection)
        self.saved = toga.DetailedList(
            on_select=self.load_connection,
            primary_action="Delete",
            on_primary_action=self.delete_connection,
            style=Pack(flex=1),
        )
        form = [
            self.c_backend,
            self.c_name,
            self.c_server,
            self.c_user,
            self.c_password,
            self.c_sid,
            self.c_computers,
            save,
        ]
        for w in form:
            w.style.update(margin=(3, 8))
        self.refresh_connections()
        return toga.Box(children=[*form, self.saved], style=Pack(direction=COLUMN, flex=1))

    def backend_key(self) -> str:
        return next(k for k, v in BACKENDS.items() if v == self.c_backend.value)

    def backend_changed(self, widget: toga.Widget) -> None:
        if self.backend_key() == "saas" and not self.c_server.value:
            self.c_server.value = "login.bigfix.com"

    def refresh_connections(self) -> None:
        self.saved.data = [
            {
                "title": c.name,
                "subtitle": f"{BACKENDS.get(c.backend, c.backend)} · {c.server} · {c.username}"
                + (" · 🔑" if self.secrets.get(c.secret_account) else ""),
                "name": c.name,
            }
            for c in self.store.load()
        ]

    def save_connection(self, widget: toga.Widget) -> None:
        name = self.c_name.value.strip() or self.backend_key()
        conn = Connection(
            name=name,
            backend=self.backend_key(),
            server=self.c_server.value.strip(),
            username=self.c_user.value.strip(),
            sid=self.c_sid.value.strip() or None,
            computers=self.c_computers.value.strip(),
        )
        self.store.upsert(conn)
        if self.c_password.value:
            self.secrets.set(conn.secret_account, self.c_password.value)
            self.c_password.value = ""
        self.refresh_connections()

    def load_connection(self, widget: toga.Widget) -> None:
        row = widget.selection
        if row is None:
            return
        conn = next((c for c in self.store.load() if c.name == row.name), None)
        if conn is None:
            return
        self.c_backend.value = BACKENDS.get(conn.backend, BACKENDS["saas"])
        self.c_name.value = conn.name
        self.c_server.value = conn.server
        self.c_user.value = conn.username
        self.c_sid.value = conn.sid or ""
        self.c_computers.value = conn.computers

    def delete_connection(self, widget: toga.Widget, row, **kwargs) -> None:
        for conn in self.store.load():
            if conn.name == row.name:
                self.secrets.delete(conn.secret_account)
        self.store.remove(row.name)
        self.refresh_connections()

    # -- About tab -------------------------------------------------------------

    def build_about(self) -> toga.Widget:
        lines = [f"{k}: {v}" for k, v in versions().items()]
        lines += [
            f"toga: {toga.__version__}",
            f"secrets: {self.secrets.name}",
            f"data: {self.store.path.parent}",
        ]
        return toga.Box(
            children=[toga.Label(line, style=Pack(margin=(4, 8))) for line in lines],
            style=Pack(direction=COLUMN),
        )


def main() -> BigFixRelevance:
    # Briefcase supplies these from pyproject.toml; given here too so a plain
    # `python -m bfrd_toga` (no briefcase metadata) runs as well.
    return BigFixRelevance("BigFix Relevance", "com.github.jgstew.bfrd_toga")
