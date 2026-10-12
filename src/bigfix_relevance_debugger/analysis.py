"""Static analysis of relevance, flattened into a view-model.

Pure and synchronous: wraps ``bigfix-relevance-analyzer`` and returns frozen
dataclasses of plain values, so the UI never touches analyzer types and this
module can be tested without a browser. No NiceGUI imports here.
"""

from __future__ import annotations

from dataclasses import dataclass

from bigfix_relevance_analyzer import (
    Dialect,
    Finding,
    LintConfig,
    analyze_relevance,
    lint_text,
)

from bigfix_relevance_debugger.upstream.relevance_analyzer import (
    DialectFit,
    client_platforms,
    dialect_fit,
    span_leads,
)

CLIENT = "client"
SESSION = "session"
AUTO = "auto"
DIALECT_MODES = {
    AUTO: (),
    CLIENT: (CLIENT,),
    SESSION: (SESSION,),
    "both": (SESSION, CLIENT),
}
"""Dialect picker choices -> what runs. Session first: its results list first."""

SESSION_BREAKDOWN_REASON = (
    "Breakdown runs client relevance; session relevance can't break down yet."
)


@dataclass(frozen=True)
class ParseErrorView:
    message: str
    offset: int
    """0-based ``str`` index into the text, usable as an editor offset."""
    line: int
    column: int


@dataclass(frozen=True)
class SpanView:
    """One underline of a finding."""

    start: int
    end: int
    """``str`` offsets into the text as typed (leading whitespace included)."""
    message: str | None
    """What this span is about, when it differs from the finding's message
    (each unknown name has its own)."""
    suggestions: tuple[str, ...]
    """"Did you mean" leads for the name written here; empty for other findings."""


@dataclass(frozen=True)
class SuggestionFix:
    """Replace every use of an unknown name with one of its leads."""

    name: str
    """The unknown name, lower-cased with single spaces."""
    suggestion: str
    text: str
    """The whole editor text with the fix applied."""


@dataclass(frozen=True)
class AutofixView:
    """The analyzer's safe fixes for the statement, all applied."""

    text: str
    """The whole editor text with every fix applied."""
    applied: int
    """How many edits that took."""


@dataclass(frozen=True)
class FindingView:
    severity: str
    """``"error"`` or ``"warning"`` (``Severity`` value)."""
    code: str
    message: str
    line: int | None
    suggestions: tuple[str, ...]
    spans: tuple[SpanView, ...]
    fixes: tuple[SuggestionFix, ...]
    """One per name and lead, in span then lead order."""


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
    dialect_mode: str
    """The picker's choice: ``"auto"`` or one of the overrides in ``DIALECT_MODES``."""
    run_dialects: tuple[str, ...]
    """What Evaluate runs: ``"session"`` and/or ``"client"``, session first."""
    dialect_reason: str
    """User-facing: why ``run_dialects`` is what it is."""
    type_summary: str | None
    """E.g. ``"singular string"``; None when the text did not parse."""
    type_errors: tuple[str, ...]
    platforms: tuple[str, ...]
    """Client platforms the expression's inspectors exist on (``"debian"``,
    ``"macos"``, ``"rhel"``, ``"ubuntu"``, ``"windows"``), sorted. Empty when
    the text did not parse."""
    complexity_score: float
    evaluation_cost: float
    costly_inspectors: tuple[str, ...]
    findings: tuple[FindingView, ...]
    autofix: AutofixView | None
    """None when the analyzer has no safe fix for the statement."""
    levels: tuple[LevelView, ...]
    can_breakdown: bool
    breakdown_disabled_reason: str | None

    @property
    def dialect(self) -> str:
        """For display: ``"client"``, ``"session"`` or ``"session + client"``."""
        return " + ".join(self.run_dialects)

    @property
    def session_optional(self) -> bool:
        """Auto added session beside client, so nobody asked for it.

        Then a missing root server configuration isn't worth an error row.
        """
        return self.dialect_mode == AUTO and self.run_dialects == (SESSION, CLIENT)


def _type_summary(check: object) -> str | None:
    value = getattr(check, "value", None)
    if value is None:
        return None
    types = value.types
    plurality = value.plurality.value
    if not types:
        return f"{plurality} (unknown type)" if plurality != "unknown" else "unknown"
    return f"{plurality} {' | '.join(sorted(types))}"


def _names(names: tuple[str, ...], limit: int = 3) -> str:
    shown = ", ".join(names[:limit])
    return shown + (f" (+{len(names) - limit} more)" if len(names) > limit else "")


def _fit_reason(fit: DialectFit) -> str:
    """Why auto picked what it did, in a phrase."""
    only = next(iter(fit.dialects)) if len(fit.dialects) == 1 else None
    match fit.basis:
        case "resolved":
            names = fit.session_only if only is Dialect.SESSION else fit.client_only
            kind = only.value if only else ""
            return f"only {kind} relevance has {_names(names)}" if names else f"{kind} relevance"
        case "type-check":
            return f"only type-checks as {only.value if only else ''} relevance"
        case "either":
            return "valid as either client or session relevance"
        case "unclear":
            return "type-checks as neither dialect; assuming client relevance"
        case "mixed":
            if fit.session_only and fit.client_only:
                return (
                    f"mixes session ({_names(fit.session_only)}) and client "
                    f"({_names(fit.client_only)}) inspectors, so trying both"
                )
            return "mixes session and client inspectors, so trying both"
        case "classified":
            return f"looks like {only.value if only else ''} relevance"
        case _:
            return "nothing says which dialect; assuming client relevance"


def _run_dialects(text: str, mode: str) -> tuple[tuple[str, ...], str]:
    if mode not in DIALECT_MODES:
        raise ValueError(f"unknown dialect mode {mode!r}")
    if mode != AUTO:
        return DIALECT_MODES[mode], f"set to {mode}"
    fit = dialect_fit(text)
    run = tuple(d for d in (SESSION, CLIENT) if Dialect(d) in fit.dialects)
    return run, _fit_reason(fit)


def _name_key(written: str) -> str:
    return " ".join(written.lower().split())


def _replace_all(text: str, spans: list[tuple[int, int]], replacement: str) -> str:
    for start, end in sorted(spans, reverse=True):
        text = text[:start] + replacement + text[end:]
    return text


def _finding_view(text: str, offset: int, finding: Finding, dialect: Dialect) -> FindingView:
    """``finding`` from ``lint_text``, whose spans index ``text.strip()``,
    re-based by ``offset`` (the leading whitespace) onto ``text``."""
    leads = span_leads(text.strip(), finding, dialect=dialect)
    spans = tuple(
        SpanView(s.start + offset, s.end + offset, s.message, lead)
        for s, lead in zip(finding.spans, leads, strict=True)
    )
    uses: dict[str, list[tuple[int, int]]] = {}
    name_leads: dict[str, tuple[str, ...]] = {}
    for span in spans:
        if span.suggestions:
            name = _name_key(text[span.start : span.end])
            uses.setdefault(name, []).append((span.start, span.end))
            name_leads.setdefault(name, span.suggestions)
    fixes = tuple(
        SuggestionFix(name, lead, _replace_all(text, uses[name], lead))
        for name, leads_ in name_leads.items()
        for lead in leads_
    )
    return FindingView(
        severity=finding.severity.value,
        code=finding.code,
        message=finding.message,
        line=finding.line,
        suggestions=tuple(finding.suggestions),
        spans=spans,
        fixes=fixes,
    )


def _autofix_view(text: str, offset: int, findings: tuple[Finding, ...]) -> AutofixView | None:
    """Every fixable finding of one statement carries the same result."""
    result = next((f.autofix for f in findings if f.autofix and f.autofix.changed), None)
    if result is None:
        return None
    end = offset + len(result.original)
    return AutofixView(
        text=text[:offset] + result.fixed + text[end:],
        applied=sum(result.applied.values()),
    )


def summarize(text: str, mode: str = AUTO) -> AnalysisView:
    """Analyze and lint ``text``, and decide which dialects to run it as.

    ``mode`` is the dialect picker: ``"auto"`` decides from the relevance
    itself, anything else in ``DIALECT_MODES`` overrides it. Analysis and lint
    run as session relevance only when that is all that will run; otherwise as
    client relevance, which is also what Breakdown evaluates. Never raises for
    bad relevance.
    """
    run_dialects, dialect_reason = _run_dialects(text, mode)
    dialect = Dialect.SESSION if run_dialects == (SESSION,) else Dialect.CLIENT
    analysis = analyze_relevance(text, dialect=dialect)
    # suggest=True is fine for one statement; it is the "did you mean" list.
    findings = lint_text(text, LintConfig(dialect=dialect, suggest=True))
    offset = len(text) - len(text.lstrip())

    parse_error = None
    if analysis.parse_error is not None:
        pe = analysis.parse_error
        parse_error = ParseErrorView(
            message=pe.message, offset=pe.offset, line=pe.line, column=pe.column
        )

    check = analysis.check
    type_errors = tuple(d.message for d in check.diagnostics) if check is not None else ()
    platforms = client_platforms(analysis)

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

    if CLIENT not in run_dialects:
        reason: str | None = SESSION_BREAKDOWN_REASON
    elif parse_error is not None:
        reason = f"Breakdown needs a clean parse: {parse_error.message}"
    elif not levels:
        reason = "Nothing to break down in this expression."
    else:
        reason = None

    complexity = analysis.complexity
    return AnalysisView(
        text=text,
        parse_error=parse_error,
        dialect_mode=mode,
        run_dialects=run_dialects,
        dialect_reason=dialect_reason,
        type_summary=_type_summary(check),
        type_errors=type_errors,
        platforms=platforms,
        complexity_score=complexity.score,
        evaluation_cost=complexity.evaluation_cost,
        costly_inspectors=tuple(complexity.costly_inspectors),
        findings=tuple(_finding_view(text, offset, f, dialect) for f in findings),
        autofix=_autofix_view(text, offset, findings),
        levels=levels,
        can_breakdown=reason is None,
        breakdown_disabled_reason=reason,
    )


def _mark(
    text: str, start: int, end: int, css_class: str, title: str | None = None
) -> dict[str, object] | None:
    """A CodeMirror mark spec, or None when the range can't be one.

    NiceGUI rejects an empty or inverted mark with a ValueError on assignment,
    so anything degenerate is dropped here rather than taking the editor down.
    ``title`` becomes the browser's hover tooltip. NiceGUI applies
    ``attributes`` unsanitized, so only ever pass ``title`` (plain text to the
    browser, never HTML) through it.
    """
    if not 0 <= start < end <= len(text):
        return None
    spec: dict[str, object] = {"kind": "mark", "from": start, "to": end, "class": css_class}
    if title is not None:
        spec["attributes"] = {"title": title}
    return spec


def editor_decorations(
    view: AnalysisView, highlight: tuple[int, int] | None = None
) -> list[dict[str, object]]:
    """CodeMirror decoration specs for ``view``: parse error, lint findings, a level span.

    Error and finding marks carry their message as a hover tooltip.

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
        marks.append(_mark(text, start, start + 1, "bfrd-parse-error", view.parse_error.message))
    for finding in view.findings:
        if finding.code == "parse-error":
            continue  # already marked above
        for span in finding.spans:
            title = span.message or finding.message
            marks.append(_mark(text, span.start, span.end, f"bfrd-{finding.severity}", title))
    if highlight is not None:
        marks.append(_mark(text, highlight[0], highlight[1], "bfrd-level"))
    return [m for m in marks if m is not None]
