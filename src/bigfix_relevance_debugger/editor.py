"""The relevance editor: NiceGUI's CodeMirror wired to the analyzer's language server.

The browser half is ``relevance_codemirror.js``, which extends NiceGUI's own
CodeMirror component, and the shared client ``upstream/web/relevance_editor.js``
(a candidate for bigfix-relevance-analyzer). Python owns the document: the
element's ``value`` is the text, and an in-process
:class:`~bigfix_relevance_debugger.upstream.relevance_analyzer.StatementSession`
answers the browser's requests against it.

Messages, over NiceGUI's event channel:

- browser -> Python: an ``lsp`` event ``{id, method, params}``, only for
  :data:`ALLOWED_REQUESTS`;
- Python -> browser: ``lspReply(id, result, error)`` and
  ``lspNotify(method, params)``, the latter for ``publishDiagnostics`` with
  the ``text`` they were computed for, so the browser can drop stale ones.

Server calls run in a worker thread, one at a time per editor.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

import nicegui
from bigfix_relevance_analyzer import Dialect
from nicegui import ui
from nicegui.events import GenericEventArguments

from bigfix_relevance_debugger.upstream.relevance_analyzer import LspError, StatementSession

logger = logging.getLogger(__name__)

ALLOWED_REQUESTS = frozenset(
    {"textDocument/completion", "textDocument/hover", "textDocument/codeAction"}
)
"""What the browser may ask. Anything else (shutdown, a document change...) is
refused: the event comes from the page, so it is untrusted input."""

METHOD_NOT_FOUND = -32601

_NICEGUI_CODEMIRROR = Path(nicegui.__file__).parent / "elements" / "codemirror"
_UPSTREAM_WEB = Path(__file__).parent / "upstream" / "web"


class RelevanceEditor(
    ui.codemirror,
    component="relevance_codemirror.js",
    esm={
        # NiceGUI's component, so ours can `extends` it.
        "nicegui-codemirror-base": str(_NICEGUI_CODEMIRROR),
        "bigfix-relevance-editor": str(_UPSTREAM_WEB),
    },
):
    """``ui.codemirror`` plus the analyzer's diagnostics, served in-process."""

    def __init__(self, value: str = "", *, dialect: str = "client", **kwargs: Any) -> None:
        super().__init__(value, **kwargs)
        self._dialect = dialect
        self._session: StatementSession | None = StatementSession(dialect=Dialect(dialect))
        self._lock = asyncio.Lock()
        self.on("lsp", self._on_lsp)

    @property
    def dialect(self) -> str:
        """``"client"`` or ``"session"``: what the language server lints as."""
        return self._dialect

    async def publish(self, dialect: str | None = None) -> None:
        """Bring the server up to date with ``value`` (and ``dialect``) and push
        its diagnostics to the browser if anything changed."""
        async with self._lock:
            if self._session is None:
                return
            if dialect is not None and dialect != self._dialect:
                # The linter's dialect is fixed per server: start a new one.
                old, self._session = self._session, StatementSession(dialect=Dialect(dialect))
                self._dialect = dialect
                await asyncio.to_thread(old.close)
            text = self.value or ""
            diagnostics = await asyncio.to_thread(self._session.sync, text)
        if diagnostics is not None:
            self.run_method(
                "lspNotify",
                "textDocument/publishDiagnostics",
                {"uri": StatementSession.URI, "diagnostics": diagnostics, "text": text},
            )

    def close(self) -> None:
        """Shut the server down. Later calls do nothing."""
        session, self._session = self._session, None
        if session is not None:
            session.close()

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
                return
            try:
                result = await asyncio.to_thread(session.request, method, params, self.value or "")
            except LspError as error:
                self._reply(id_, error={"code": error.code, "message": error.message})
                return
        self._reply(id_, result)

    def _reply(self, id_: int, result: Any = None, *, error: dict[str, Any] | None = None) -> None:
        self.run_method("lspReply", id_, result, error)
