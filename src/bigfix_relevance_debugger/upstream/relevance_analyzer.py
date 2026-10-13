"""Candidates for bigfix-relevance-analyzer.

See ``upstream/__init__.py`` for the rules.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from bigfix_relevance_analyzer import (
    Dialect,
    Finding,
    LintConfig,
    Outcome,
    Plurality,
    RelevanceAnalysis,
    analyze_relevance,
    interpret_count_results,
    suggest,
)
from bigfix_relevance_analyzer.lsp import DocumentLinter, Server


def client_platforms(analysis: RelevanceAnalysis) -> tuple[str, ...]:
    """Client platforms the expression's inspectors exist on, sorted.

    The type check's platform set also carries session environments
    (``"session:rest_api"``, ...), which no endpoint can match. Empty when the
    text did not parse.

    Upstream: a ``RelevanceValue.client_platforms`` property.
    """
    if analysis.check is None:
        return ()
    return tuple(sorted(p for p in analysis.check.value.platforms if not p.startswith("session:")))


@dataclass(frozen=True)
class CountSummary:
    """One count probe's rows reduced to a single per-level answer.

    ``outcome`` extends :class:`Outcome`'s values with ``"no-context"``: the
    probe answered nothing at all, because its context produced no objects.
    """

    outcome: str
    total: int | None
    """Objects summed over every context; None when nothing was evaluable."""
    contexts: int
    not_evaluable: int


def summarize_count_results(rows: Sequence[str]) -> CountSummary:
    """Aggregate :func:`interpret_count_results` over every context object.

    A level with some contexts not evaluable and others counted reports the
    counted total; it is ``not-evaluable`` only when no context was. Raises
    ``ValueError`` for a row that is not a count, as
    :func:`interpret_count_results` does.

    Upstream: ``breakdown.py``, beside ``interpret_count_results``.
    """
    outcomes = interpret_count_results(rows)
    contexts = len(outcomes)
    not_evaluable = sum(o.outcome is Outcome.NOT_EVALUABLE for o in outcomes)
    if contexts == 0:
        return CountSummary("no-context", None, 0, 0)
    if not_evaluable == contexts:
        return CountSummary(Outcome.NOT_EVALUABLE.value, None, contexts, not_evaluable)
    total = sum(o.count or 0 for o in outcomes if o.outcome is not Outcome.NOT_EVALUABLE)
    outcome = Outcome.COUNT if total > 0 else Outcome.EMPTY_OR_ERROR
    return CountSummary(outcome.value, total, contexts, not_evaluable)


@dataclass(frozen=True)
class DialectFit:
    """Which dialects a statement can run as: the ones worth evaluating it in.

    ``basis`` says how that was decided:

    - ``"resolved"``: every resolved reference belongs to the one dialect
    - ``"type-check"``: references exist in both, but only one dialect's type
      check accepts the statement (``name of operating system``: session has
      ``operating system of <bes computer>``, never a bare ``operating system``)
    - ``"either"``: both type checks accept it, e.g. ``now``
    - ``"unclear"``: neither type check accepts it (a typo) and the text gives
      no hint: client, as for a statement that doesn't parse
    - ``"mixed"``: it uses references from both dialects (``Dialect.UNCERTAIN``)
    - ``"classified"``: it doesn't parse or type-check, but the text classifier
      has a view (``bes <word>`` reads as session)
    - ``"default"``: it doesn't parse and nothing hints at session: client
    """

    dialects: frozenset[Dialect]
    """``CLIENT`` and/or ``SESSION``; never empty."""
    basis: str
    client_only: tuple[str, ...] = ()
    """References only client relevance has."""
    session_only: tuple[str, ...] = ()
    """References only session relevance has."""


def _type_checks(text: str, dialect: Dialect) -> bool:
    check = analyze_relevance(text, dialect=dialect).check
    return (
        check is not None
        and not check.diagnostics
        and check.value.plurality is not Plurality.UNKNOWN
    )


def dialect_fit(text: str) -> DialectFit:
    """Decide which dialects ``text`` can run as, from its references and type checks.

    ``RelevanceAnalysis.resolved_dialect`` intersects dialects by reference
    *name*, so it reports ``BOTH`` for some client-only statements; type
    checking under each dialect settles those.

    Upstream: ``resolved_dialect`` itself could consult the type check.
    """
    both = frozenset({Dialect.CLIENT, Dialect.SESSION})
    analysis = analyze_relevance(text)
    client_only = analysis.references_only_in(Dialect.CLIENT)
    session_only = analysis.references_only_in(Dialect.SESSION)

    def fit(dialects: frozenset[Dialect], basis: str) -> DialectFit:
        return DialectFit(dialects, basis, client_only, session_only)

    classified = analysis.classified_dialect
    resolved = analysis.resolved_dialect
    if resolved in (Dialect.CLIENT, Dialect.SESSION):
        return fit(frozenset({resolved}), "resolved")
    if resolved is Dialect.UNCERTAIN:
        return fit(both, "mixed")
    if resolved is Dialect.BOTH:
        ok = frozenset(d for d in both if _type_checks(text, d))
        if len(ok) == 1:
            return fit(ok, "type-check")
        if ok:
            return fit(both, "either")
        if classified in (Dialect.CLIENT, Dialect.SESSION):
            return fit(frozenset({classified}), "classified")
        return fit(frozenset({Dialect.CLIENT}), "unclear")
    # Unparsed, or nothing resolved: fall back on the text classifier.
    if classified in (Dialect.CLIENT, Dialect.SESSION):
        return fit(frozenset({classified}), "classified")
    if classified is Dialect.UNCERTAIN:
        return fit(both, "mixed")
    return fit(frozenset({Dialect.CLIENT}), "default")


def name_key(written: str) -> str:
    """An inspector name as written, folded for comparison: lower case, single spaces.

    ``Operating\n  Sytem`` and ``operating sytem`` are the same name to
    :func:`suggest`; this is the key for "the same name" everywhere else.
    """
    return " ".join(written.lower().split())


@lru_cache(maxsize=1024)
def _leads(name: str, dialect: Dialect | None) -> tuple[str, ...]:
    # suggest() is a fuzzy search over every inspector, milliseconds a call,
    # and an editor asks again about the same names on every keystroke.
    return suggest(name, dialect=dialect)


def span_leads(
    statement: str, finding: Finding, *, dialect: Dialect | None
) -> tuple[tuple[str, ...], ...]:
    """The "did you mean" leads for each of ``finding``'s spans, in span order.

    An ``unknown-inspector`` finding covers every unknown name in the
    statement, and its :attr:`Finding.suggestions` merges all their leads, so
    an editor can't tell which lead belongs to which underline. Each span is
    one use of one name; this asks :func:`suggest` about the name as written
    there (by :func:`name_key`), remembering answers across calls, so every
    use of a name shares one tuple. Any other finding gets ``()`` per span.

    ``statement`` is the text the spans index into (for :func:`lint_text`,
    ``text.strip()``), and ``dialect`` is ``LintConfig.dialect`` as linted.
    ``None`` is resolved the way lint resolves it: the statement's own
    dialect unless that was only assumed, else every dialect.

    Upstream: a ``suggestions`` field on ``TextSpan``, filled where lint
    already computes each name's leads (which also saves this second search).
    """
    if finding.code != "unknown-inspector":
        return tuple(() for _ in finding.spans)
    if dialect is None:
        report = analyze_relevance(statement)
        dialect = None if report.dialect_assumed else report.dialect
    return tuple(_leads(name_key(statement[s.start : s.end]), dialect) for s in finding.spans)


class LspError(Exception):
    """A JSON-RPC error response from the language server."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(f"{message} (code {code})")
        self.code = code
        self.message = message


class StatementSession:
    """One relevance statement held in an in-process :class:`lsp.Server`.

    For an editor that is not a language client (a web page, a notebook):
    the caller owns the text and hands it to every call, and the session
    keeps the server's copy current, opening the document on first use and
    sending a full-text change whenever the text differs. So a request is
    always answered against the text it was asked about.

    The document is ``URI`` with language ``bigfix-relevance``: one plain
    statement, as a ``.rel`` file. ``dialect`` forces it, as
    ``LintConfig.dialect`` does; ``None`` lets the server decide.

    Positions are LSP's: 0-based line and UTF-16 character, which is also
    what CodeMirror 6 counts in. Not thread-safe: one caller at a time.

    Upstream: ``lsp``, beside :class:`Server`, for the playground and any
    other embedder.
    """

    URI = "untitled:statement"
    LANGUAGE_ID = "bigfix-relevance"
    CAPABILITIES: Mapping[str, Any] = {
        "textDocument": {
            "codeAction": {
                "codeActionLiteralSupport": {
                    "codeActionKind": {"valueSet": ["quickfix", "source.fixAll.bigfix-relevance"]}
                }
            },
            # No snippet support: plain insert text is all a minimal client handles.
            "completion": {"completionItem": {"snippetSupport": False}},
        },
        # Plain {uri: [TextEdit]} edits; this session never versions edits.
        "workspace": {"workspaceEdit": {"documentChanges": False}},
    }

    def __init__(self, *, dialect: Dialect | None = None) -> None:
        self.dialect = dialect
        config = LintConfig(suggest=True, dialect=dialect)
        self._server = Server(linter=DocumentLinter(config=config))
        self._ids = 0
        self._text: str | None = None
        self.version = 0
        """Bumped on every text the server is sent; 0 before the first."""
        self.diagnostics: list[dict[str, Any]] = []
        """The server's diagnostics for the text it holds, however it got there
        (:meth:`sync` or the sync inside :meth:`request`)."""
        self._closed = False
        self._call("initialize", {"processId": None, "capabilities": self.CAPABILITIES})
        self._notify("initialized", {})

    def sync(self, text: str) -> list[dict[str, Any]] | None:
        """Make the server's copy ``text``. Its diagnostics if that changed it, else None.

        None means "nothing new", not "no diagnostics": :attr:`diagnostics`
        always has the latest.
        """
        if self._closed:
            raise RuntimeError("StatementSession is closed")
        if text == self._text:
            return None
        self.version += 1
        if self._text is None:
            document = {
                "uri": self.URI,
                "languageId": self.LANGUAGE_ID,
                "version": self.version,
                "text": text,
            }
            replies = self._notify("textDocument/didOpen", {"textDocument": document})
        else:
            replies = self._notify(
                "textDocument/didChange",
                {
                    "textDocument": {"uri": self.URI, "version": self.version},
                    "contentChanges": [{"text": text}],
                },
            )
        self._text = text
        self.diagnostics = next(
            (
                list(reply["params"]["diagnostics"])
                for reply in replies
                if reply.get("method") == "textDocument/publishDiagnostics"
            ),
            [],
        )
        return list(self.diagnostics)

    def request(self, method: str, params: Mapping[str, Any], text: str) -> Any:
        """``method``'s result for the document holding ``text``.

        ``params`` without ``textDocument``; the session adds it. Raises
        :class:`LspError` for an error response.
        """
        self.sync(text)
        return self._call(method, {**params, "textDocument": {"uri": self.URI}})

    def close(self) -> None:
        """Close the document and shut the server down. Safe to call twice."""
        if self._closed:
            return
        if self._text is not None:
            self._notify("textDocument/didClose", {"textDocument": {"uri": self.URI}})
        self._call("shutdown", None)
        self._notify("exit", None)
        self._closed = True

    def _call(self, method: str, params: Mapping[str, Any] | None) -> Any:
        self._ids += 1
        message: dict[str, Any] = {"jsonrpc": "2.0", "id": self._ids, "method": method}
        if params is not None:
            message["params"] = params
        for reply in self._server.handle(message):
            if reply.get("id") != self._ids:
                continue
            if "error" in reply:
                error = reply["error"]
                raise LspError(error["code"], error["message"])
            return reply.get("result")
        raise LspError(-32603, f"no response to {method}")

    def _notify(self, method: str, params: Mapping[str, Any] | None) -> list[dict[str, Any]]:
        message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        return self._server.handle(message)
