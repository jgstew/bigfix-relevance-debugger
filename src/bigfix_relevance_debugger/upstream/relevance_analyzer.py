"""Candidates for bigfix-relevance-analyzer.

See ``upstream/__init__.py`` for the rules.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from bigfix_relevance_analyzer import (
    Dialect,
    Outcome,
    Plurality,
    RelevanceAnalysis,
    analyze_relevance,
    interpret_count_results,
)


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
