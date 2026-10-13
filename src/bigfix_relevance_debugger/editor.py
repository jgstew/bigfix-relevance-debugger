"""The relevance editor: NiceGUI's CodeMirror wired to the analyzer's language server.

The browser half is ``relevance_codemirror.js``, which extends NiceGUI's own
CodeMirror component, plus the analyzer wheel's ``web/relevance_editor.js``
(the shared language-server client) and ``web/relevance_language.js`` (its
highlighting). Python owns the document: the element's ``value`` is the text,
and an in-process :class:`bigfix_relevance_analyzer.lsp.StatementSession`
answers the browser's requests against it.

Messages, over NiceGUI's event channel:

- browser -> Python: an ``lsp`` event ``{id, method, params}``, only for
  :data:`ALLOWED_REQUESTS`;
- browser -> Python: ``lspReady`` when the browser editor (re)mounts, so
  Python re-sends the diagnostics it has;
- Python -> browser: ``lspReply(id, result, error)``, for every request, and
  ``lspNotify(method, params)``, for ``publishDiagnostics`` with the ``text``
  they were computed for, so the browser can drop stale ones.

Server calls run in a worker thread, one at a time per editor, shutdown
included.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, NoReturn

import bigfix_relevance_analyzer
import nicegui
from bigfix_relevance_analyzer import Dialect
from bigfix_relevance_analyzer.lsp import LspError, StatementSession
from nicegui import background_tasks, ui
from nicegui.events import GenericEventArguments

logger = logging.getLogger(__name__)

ALLOWED_REQUESTS = frozenset(
    {"textDocument/completion", "textDocument/hover", "textDocument/codeAction"}
)
"""What the browser may ask. Anything else (shutdown, a document change...) is
refused: the event comes from the page, so it is untrusted input."""

METHOD_NOT_FOUND = -32601
REQUEST_CANCELLED = -32800

_NICEGUI_CODEMIRROR = Path(nicegui.__file__).parent / "elements" / "codemirror"
_ANALYZER_WEB = Path(bigfix_relevance_analyzer.__file__).parent / "web"
"""The analyzer's browser modules. A real directory: installs are unpacked."""


class RelevanceEditor(
    ui.codemirror,
    component="relevance_codemirror.js",
    esm={
        # NiceGUI's component, so ours can `extends` it.
        "nicegui-codemirror-base": str(_NICEGUI_CODEMIRROR),
        "bigfix-relevance-editor": str(_ANALYZER_WEB),
    },
):
    """``ui.codemirror`` plus the analyzer's diagnostics, served in-process."""

    _ONE_LANGUAGE = "RelevanceEditor highlights relevance itself; a language would add a second one"

    def __init__(self, value: str = "", *, dialect: str = "client", **kwargs: Any) -> None:
        if "language" in kwargs:
            raise TypeError(self._ONE_LANGUAGE)
        super().__init__(value, **kwargs)
        self._dialect = dialect
        self._session: StatementSession | None = StatementSession(dialect=Dialect(dialect))
        self._sent_text: str | None = None
        """The text whose diagnostics the browser last received; None for none."""
        self._lock = asyncio.Lock()
        """One server call at a time, shutdown included: the session isn't thread-safe."""
        self.on("lsp", self._on_lsp)
        self.on("lspReady", self._on_ready)

    @property
    def language(self) -> str:
        """Always unset: the component's relevanceLanguage(CM) is the only language."""
        return super().language

    @language.setter
    def language(self, language: Any = None) -> None:
        raise TypeError(self._ONE_LANGUAGE)

    def set_language(self, language: Any = None) -> NoReturn:
        raise TypeError(self._ONE_LANGUAGE)

    @property
    def dialect(self) -> str:
        """``"client"`` or ``"session"``: what the language server lints as."""
        return self._dialect

    async def publish(self, dialect: str | None = None) -> None:
        """Bring the server up to date with ``value`` (and ``dialect``) and push
        its diagnostics to the browser unless it already has them.

        Safe to cancel: what decides whether to send is the text the browser
        last *received*, so an interrupted publish is simply redone next time.
        """
        async with self._lock:
            session = self._session
            if session is None:
                return
            if dialect is not None and dialect != self._dialect:
                # The linter's dialect is fixed per server: start a new one.
                old, session = session, StatementSession(dialect=Dialect(dialect))
                self._session, self._dialect = session, dialect
                self._sent_text = None
                await asyncio.to_thread(old.close)
            text = self.value or ""
            await asyncio.to_thread(session.sync, text)
            if self._session is not session or text == self._sent_text:
                return  # closed or replaced meanwhile, or nothing new for the browser
            diagnostics = session.diagnostics
            self._sent_text = text
        if not self.is_deleted:
            self.run_method(
                "lspNotify",
                "textDocument/publishDiagnostics",
                {"uri": StatementSession.URI, "diagnostics": diagnostics, "text": text},
            )

    def close(self) -> None:
        """Shut the server down, once whatever call holds it is done. Later calls do nothing."""
        session, self._session = self._session, None
        if session is not None:
            background_tasks.create(self._shutdown(session), name="relevance-editor-close")

    async def _shutdown(self, session: StatementSession) -> None:
        async with self._lock:
            await asyncio.to_thread(session.close)

    async def _on_ready(self, _: GenericEventArguments) -> None:
        # A (re)mounted browser editor has no diagnostics yet.
        self._sent_text = None
        await self.publish()

    async def _on_lsp(self, e: GenericEventArguments) -> None:
        args = e.args
        if not isinstance(args, dict):
            return
        id_, method, params = args.get("id"), args.get("method"), args.get("params")
        if not isinstance(id_, int) or not isinstance(method, str) or not isinstance(params, dict):
            logger.debug("ignoring malformed lsp event: %r", args)
            return
        if method not in ALLOWED_REQUESTS:
            self._reply(id_, error={"code": METHOD_NOT_FOUND, "message": f"not allowed: {method}"})
            return
        async with self._lock:
            session = self._session
            if session is None:
                self._reply(id_, error={"code": REQUEST_CANCELLED, "message": "editor closed"})
                return
            try:
                result = await asyncio.to_thread(session.request, method, params, self.value or "")
            except LspError as error:
                self._reply(id_, error={"code": error.code, "message": error.message})
                return
        self._reply(id_, result)

    def _reply(self, id_: int, result: Any = None, *, error: dict[str, Any] | None = None) -> None:
        if not self.is_deleted:
            self.run_method("lspReply", id_, result, error)
