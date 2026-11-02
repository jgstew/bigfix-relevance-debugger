"""Static analysis for the editor: dialect, type, findings and highlight spans.

Pure and synchronous, and cheap enough (tens of ms) to run on every pause in
typing. Returns plain frozen dataclasses so a UI never touches analyzer types.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from bigfix_relevance_analyzer import (
    Dialect,
    LintConfig,
    Plurality,
    TokenKind,
    analyze_relevance,
    lint_text,
    tokenize,
)

CLIENT = "client"
SESSION = "session"

# Relevance's grammar words. The analyzer's tokenizer only knows "word", so
# the highlighter tells keywords from inspector names with this list.
KEYWORDS = frozenset(
    [
        "of",
        "whose",
        "it",
        "as",
        "if",
        "then",
        "else",
        "and",
        "or",
        "not",
        "exists",
        "is",
        "are",
        "equal",
        "contains",
        "starts",
        "ends",
        "with",
        "greater",
        "less",
        "than",
        "mod",
        "there",
        "does",
        "do",
        "matches",
        "nothing",
        "true",
        "false",
        "number",
    ]
)


@dataclass(frozen=True)
class Span:
    """A highlight range. ``kind`` is one of the ``SPAN_KINDS``."""

    start: int
    end: int
    kind: str


SPAN_KINDS = ("keyword", "string", "number", "comment", "punct", "error", "warning")


@dataclass(frozen=True)
class FindingView:
    severity: str  # "error" | "warning"
    code: str
    message: str
    start: int | None
    end: int | None
    suggestions: tuple[str, ...]


@dataclass(frozen=True)
class Analysis:
    text: str
    dialects: tuple[str, ...]
    """What Evaluate runs, session first. Never empty for non-blank text."""
    dialect_reason: str
    parse_error: str | None
    type_summary: str | None
    findings: tuple[FindingView, ...]
    spans: tuple[Span, ...]

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    @property
    def summary(self) -> str:
        """One line for a status bar."""
        if not self.text.strip():
            return "Type some relevance."
        parts = [" + ".join(self.dialects) + f" ({self.dialect_reason})"]
        if self.parse_error:
            parts.append(f"parse error: {self.parse_error}")
        elif self.type_summary:
            parts.append(self.type_summary)
        if self.findings:
            parts.append(f"{len(self.findings)} finding(s)")
        return " · ".join(parts)


def _type_checks(text: str, dialect: Dialect) -> bool:
    check = analyze_relevance(text, dialect=dialect).check
    return (
        check is not None
        and not check.diagnostics
        and check.value.plurality is not Plurality.UNKNOWN
    )


def run_dialects(text: str) -> tuple[tuple[str, ...], str]:
    """Which dialects to evaluate ``text`` in, and why.

    A trimmed copy of the desktop app's ``upstream.relevance_analyzer.dialect_fit``.
    """
    analysis = analyze_relevance(text)
    resolved = analysis.resolved_dialect
    if resolved is Dialect.CLIENT:
        return (CLIENT,), "client-only inspectors"
    if resolved is Dialect.SESSION:
        return (SESSION,), "session-only inspectors"
    if resolved is Dialect.UNCERTAIN:
        return (SESSION, CLIENT), "mixes both dialects"
    if resolved is Dialect.BOTH:
        ok = [d for d in (SESSION, CLIENT) if _type_checks(text, Dialect(d))]
        if len(ok) == 1:
            return (ok[0],), f"only type-checks as {ok[0]}"
        if ok:
            return (SESSION, CLIENT), "valid in either"
    classified = analysis.classified_dialect
    if classified is Dialect.SESSION:
        return (SESSION,), "reads as session"
    return (CLIENT,), "default"


def _type_summary(text: str, dialect: Dialect) -> str | None:
    check = analyze_relevance(text, dialect=dialect).check
    if check is None or check.diagnostics:
        return None
    value = check.value
    if not value.types:
        return None
    return f"{value.plurality.value} {' | '.join(sorted(value.types))}"


def highlight(text: str) -> list[Span]:
    """Syntax spans from the analyzer's own tokenizer (no Pygments lexer needed)."""
    spans: list[Span] = []
    for token in tokenize(text):
        kind = token.kind
        if kind is TokenKind.STRING:
            name = "string"
        elif kind is TokenKind.NUMBER:
            name = "number"
        elif kind is TokenKind.COMMENT:
            name = "comment"
        elif kind is TokenKind.PUNCT:
            name = "punct"
        elif kind is TokenKind.ERROR:
            name = "error"
        elif kind is TokenKind.WORD and token.text.lower() in KEYWORDS:
            name = "keyword"
        else:
            continue
        spans.append(Span(token.offset, token.offset + len(token.text), name))
    return spans


def analyze(text: str) -> Analysis:
    """Never raises for bad relevance."""
    if not text.strip():
        return Analysis(text, (), "", None, None, (), ())
    dialects, reason = run_dialects(text)
    dialect = Dialect.SESSION if dialects == (SESSION,) else Dialect.CLIENT
    result = analyze_relevance(text, dialect=dialect)
    parse_error = None
    spans = highlight(text)
    if result.parse_error is not None:
        pe = result.parse_error
        parse_error = f"col {pe.column}: {pe.message}"
        spans.append(Span(pe.offset, min(len(text), pe.offset + 1), "error"))
    findings: list[FindingView] = []
    for f in lint_text(text, LintConfig(dialect=dialect, suggest=True)):
        if f.code == "parse-error":
            continue  # already reported as parse_error
        start = f.spans[0].start if f.spans else None
        end = f.spans[0].end if f.spans else None
        severity = f.severity.value
        findings.append(FindingView(severity, f.code, f.message, start, end, f.suggestions))
        if start is not None and end is not None:
            spans.append(Span(start, end, "error" if severity == "error" else "warning"))
    return Analysis(
        text=text,
        dialects=dialects,
        dialect_reason=reason,
        parse_error=parse_error,
        type_summary=None if parse_error else _type_summary(text, dialect),
        findings=tuple(findings),
        spans=tuple(spans),
    )
