"""Static analysis of relevance, flattened into a view-model.

Pure and synchronous: wraps ``bigfix-relevance-analyzer`` and returns frozen
dataclasses of plain values, so the UI never touches analyzer types and this
module can be tested without a browser. No NiceGUI imports here.
"""

from __future__ import annotations

import re
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
    """Replace every use of an unknown name with one of its leads.

    Applied on demand with :func:`suggestion_fix`.
    """

    name: str
    """The unknown name, lower-cased with single spaces."""
    suggestion: str
    label: str
    """Button text: the lead, prefixed ``name →`` when the finding has several names."""
    spans: tuple[tuple[int, int], ...]
    """Every use of ``name``: ``str`` offsets into the text as typed."""


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
    lint_dialect: str
    """What analysis and lint ran as: ``"session"`` when only session runs, else
    ``"client"``. The editor's language server is forced to the same."""
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


def name_key(written: str) -> str:
    """An inspector name as written, folded for comparison: lower case, single spaces.

    ``Operating\n  Sytem`` and ``operating sytem`` are the same name to
    :func:`~bigfix_relevance_analyzer.suggest`; this is the key for "the same
    name" everywhere else.
    """
    return " ".join(written.lower().split())


def _finding_view(text: str, offset: int, finding: Finding) -> FindingView:
    """``finding`` from ``lint_text``, whose spans index ``text.strip()``,
    re-based by ``offset`` (the leading whitespace) onto ``text``.

    Each span carries its own name's leads (``TextSpan.suggestions``);
    ``Finding.suggestions`` merges every name's, sorted.
    """
    spans = tuple(
        SpanView(s.start + offset, s.end + offset, s.message, s.suggestions) for s in finding.spans
    )
    uses: dict[str, list[tuple[int, int]]] = {}
    name_leads: dict[str, tuple[str, ...]] = {}
    for span in spans:
        if span.suggestions:
            name = name_key(text[span.start : span.end])
            uses.setdefault(name, []).append((span.start, span.end))
            name_leads.setdefault(name, span.suggestions)
    several = len(name_leads) > 1
    fixes = tuple(
        SuggestionFix(
            name=name,
            suggestion=lead,
            label=f"{name} \u2192 {lead}" if several else lead,
            spans=tuple(uses[name]),
        )
        for name, leads_ in name_leads.items()
        for lead in leads_
    )
    return FindingView(
        severity=finding.severity.value,
        code=finding.code,
        message=finding.message,
        # lint_text counts lines from text.strip(); count the text as typed.
        line=None if finding.line is None else finding.line + text[:offset].count("\n"),
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
        lint_dialect=dialect.value,
        type_summary=_type_summary(check),
        type_errors=type_errors,
        platforms=platforms,
        complexity_score=complexity.score,
        evaluation_cost=complexity.evaluation_cost,
        costly_inspectors=tuple(complexity.costly_inspectors),
        findings=tuple(_finding_view(text, offset, f) for f in findings),
        autofix=_autofix_view(text, offset, findings),
        levels=levels,
        can_breakdown=reason is None,
        breakdown_disabled_reason=reason,
    )


def _fixes(view: AnalysisView) -> list[SuggestionFix]:
    return [fix for finding in view.findings for fix in finding.fixes]


def suggestion_fix(view: AnalysisView, name: str, suggestion: str) -> str | None:
    """``view.text`` with every use of ``name`` replaced by ``suggestion``.

    None when ``view`` no longer offers that fix: the name was edited away,
    or its leads changed (a different dialect suggests other names).
    """
    fix = next((f for f in _fixes(view) if (f.name, f.suggestion) == (name, suggestion)), None)
    if fix is None:
        return None
    text = view.text
    for start, end in sorted(fix.spans, reverse=True):
        text = text[:start] + suggestion + text[end:]
    return text


def missing_fix_reason(view: AnalysisView, name: str, suggestion: str) -> str:
    """Why :func:`suggestion_fix` found nothing, for the user.

    Asks the text, not the fixes, whether ``name`` is still there: it can be
    and have no fixes, when it is valid in the dialect now analysed. A
    whole-word match on lower-cased, single-spaced text: approximate (it
    also finds the name inside a string), but it only picks the message.
    """
    if re.search(rf"(?<!\w){re.escape(name)}(?!\w)", name_key(view.text)):
        return f"{suggestion} no longer applies to {name}."
    return f"{name} is no longer in the text."


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
    """CodeMirror decoration specs for ``view``: the highlighted level span, if any.

    Parse errors and lint findings are not marked here: the editor's language
    server underlines them, with their messages (see ``editor.py``).

    Offsets are ``str`` indices into ``view.text``, which is what NiceGUI's
    ``codemirror.decorations`` expects. The page ships the ``bfrd-level`` style.
    """
    if highlight is None:
        return []
    mark = _mark(view.text, highlight[0], highlight[1], "bfrd-level")
    return [mark] if mark is not None else []
