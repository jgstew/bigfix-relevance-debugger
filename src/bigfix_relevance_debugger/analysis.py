"""Static analysis of client relevance, flattened into a view-model.

Pure and synchronous: wraps ``bigfix-relevance-analyzer`` and returns frozen
dataclasses of plain values, so the UI never touches analyzer types and this
module can be tested without a browser. No NiceGUI imports here.
"""

from __future__ import annotations

from dataclasses import dataclass

from bigfix_relevance_analyzer import (
    Dialect,
    LintConfig,
    analyze_relevance,
    classify_relevance_dialect,
    lint_text,
)


@dataclass(frozen=True)
class ParseErrorView:
    message: str
    offset: int
    """0-based ``str`` index into the text, usable as an editor offset."""
    line: int
    column: int


@dataclass(frozen=True)
class FindingView:
    severity: str
    """``"error"`` or ``"warning"`` (``Severity`` value)."""
    code: str
    message: str
    line: int | None
    suggestions: tuple[str, ...]
    spans: tuple[tuple[int, int], ...]
    """``(start, end)`` ``str`` offsets into the text."""


@dataclass(frozen=True)
class LevelView:
    index: int
    label: str
    start: int
    end: int
    line: int
    column: int
    probe: str
    """Count-probe relevance to evaluate for this level."""
    unfiltered_probe: str | None
    """Set on ``whose`` levels: the same collection without its filter."""
    is_whole_expression: bool


@dataclass(frozen=True)
class AnalysisView:
    text: str
    parse_error: ParseErrorView | None
    dialect: str
    """``"client"``, ``"session"``, ``"uncertain"`` or ``"neutral"`` (no opinion)."""
    type_summary: str | None
    """E.g. ``"singular string"``; None when the text did not parse."""
    type_errors: tuple[str, ...]
    complexity_score: float
    evaluation_cost: float
    costly_inspectors: tuple[str, ...]
    findings: tuple[FindingView, ...]
    levels: tuple[LevelView, ...]
    can_breakdown: bool
    breakdown_disabled_reason: str | None


def _type_summary(check: object) -> str | None:
    value = getattr(check, "value", None)
    if value is None:
        return None
    types = value.types
    plurality = value.plurality.value
    if not types:
        return f"{plurality} (unknown type)" if plurality != "unknown" else "unknown"
    return f"{plurality} {' | '.join(sorted(types))}"


def summarize(text: str) -> AnalysisView:
    """Analyze and lint ``text`` as client relevance. Never raises for bad relevance."""
    analysis = analyze_relevance(text, dialect=Dialect.CLIENT)
    # suggest=True is fine for one statement; it is the "did you mean" list.
    findings = lint_text(text, LintConfig(dialect=Dialect.CLIENT, suggest=True))

    parse_error = None
    if analysis.parse_error is not None:
        pe = analysis.parse_error
        parse_error = ParseErrorView(
            message=pe.message, offset=pe.offset, line=pe.line, column=pe.column
        )

    # analyze_relevance only classifies when no dialect is requested, and this
    # tool always requests CLIENT, so ask the classifier directly for display.
    classified = classify_relevance_dialect(text)
    dialect = classified.value if classified is not None else "neutral"

    check = analysis.check
    type_errors = tuple(d.message for d in check.diagnostics) if check is not None else ()

    levels = tuple(
        LevelView(
            index=i,
            label=level.label,
            start=level.span.start,
            end=level.span.end,
            line=level.span.line,
            column=level.span.column,
            probe=level.probe.relevance,
            unfiltered_probe=level.unfiltered.relevance if level.unfiltered else None,
            # The analyzer emits the whole expression first, then the rest.
            is_whole_expression=i == 0,
        )
        for i, level in enumerate(analysis.levels)
    )

    if parse_error is not None:
        reason: str | None = f"Breakdown needs a clean parse: {parse_error.message}"
    elif not levels:
        reason = "Nothing to break down in this expression."
    else:
        reason = None

    complexity = analysis.complexity
    return AnalysisView(
        text=text,
        parse_error=parse_error,
        dialect=dialect,
        type_summary=_type_summary(check),
        type_errors=type_errors,
        complexity_score=complexity.score,
        evaluation_cost=complexity.evaluation_cost,
        costly_inspectors=tuple(complexity.costly_inspectors),
        findings=tuple(
            FindingView(
                severity=f.severity.value,
                code=f.code,
                message=f.message,
                line=f.line,
                suggestions=tuple(f.suggestions),
                spans=tuple((s.start, s.end) for s in f.spans),
            )
            for f in findings
        ),
        levels=levels,
        can_breakdown=reason is None,
        breakdown_disabled_reason=reason,
    )


def _mark(text: str, start: int, end: int, css_class: str) -> dict[str, object] | None:
    """A CodeMirror mark spec, or None when the range can't be one.

    NiceGUI rejects an empty or inverted mark with a ValueError on assignment,
    so anything degenerate is dropped here rather than taking the editor down.
    """
    if not 0 <= start < end <= len(text):
        return None
    return {"kind": "mark", "from": start, "to": end, "class": css_class}


def editor_decorations(
    view: AnalysisView, highlight: tuple[int, int] | None = None
) -> list[dict[str, object]]:
    """CodeMirror decoration specs for ``view``: parse error, lint findings, a level span.

    Offsets are ``str`` indices into ``view.text``, which is what NiceGUI's
    ``codemirror.decorations`` expects. CSS classes are ``bfrd-*``; the page
    ships the styles.
    """
    text = view.text
    marks: list[dict[str, object] | None] = []
    if view.parse_error is not None:
        # An error at end of text (e.g. a missing ')') has nothing after it to
        # mark, so mark the last character instead.
        start = min(view.parse_error.offset, len(text) - 1)
        marks.append(_mark(text, start, start + 1, "bfrd-parse-error"))
    for finding in view.findings:
        if finding.code == "parse-error":
            continue  # already marked above
        for start, end in finding.spans:
            marks.append(_mark(text, start, end, f"bfrd-{finding.severity}"))
    if highlight is not None:
        marks.append(_mark(text, highlight[0], highlight[1], "bfrd-level"))
    return [m for m in marks if m is not None]
