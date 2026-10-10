"""Candidates for bigfix-relevance-analyzer.

See ``upstream/__init__.py`` for the rules.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from bigfix_relevance_analyzer import Outcome, RelevanceAnalysis, interpret_count_results


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
