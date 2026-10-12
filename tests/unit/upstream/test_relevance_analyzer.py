"""Tests for upstream/relevance_analyzer.py.

Written to move into bigfix-relevance-analyzer's own suite together with the
module: only that library's public API and the stdlib are used.
"""

from __future__ import annotations

import pytest
from bigfix_relevance_analyzer import Dialect, LintConfig, analyze_relevance, lint_text, suggest

from bigfix_relevance_debugger.upstream.relevance_analyzer import (
    CountSummary,
    DialectFit,
    client_platforms,
    dialect_fit,
    span_leads,
    summarize_count_results,
)


def platforms_of(text):
    return client_platforms(analyze_relevance(text, dialect=Dialect.CLIENT))


def test_platform_specific_expression():
    assert platforms_of('exists key "HKLM\\\\Software" of registry') == ("windows",)


def test_portable_expression_lists_every_client_platform():
    assert platforms_of("name of operating system") == (
        "debian",
        "macos",
        "rhel",
        "ubuntu",
        "windows",
    )


def test_session_environments_are_excluded():
    platforms = platforms_of('exists debianpackage "bash"')

    assert platforms
    assert not any(p.startswith("session:") for p in platforms)


def test_unparseable_expression_has_no_platforms():
    assert platforms_of("name of (operating system") == ()


@pytest.mark.parametrize(
    ("rows", "expected"),
    [
        (["25"], CountSummary("count", 25, 1, 0)),
        (["3", "0", "4"], CountSummary("count", 7, 3, 0)),
        (["0"], CountSummary("empty-or-error", 0, 1, 0)),
        (["-1"], CountSummary("not-evaluable", None, 1, 1)),
        (["-1", "-1"], CountSummary("not-evaluable", None, 2, 2)),
        # Some contexts not evaluable, the rest produced objects.
        (["-1", "5"], CountSummary("count", 5, 2, 1)),
        (["-1", "0"], CountSummary("empty-or-error", 0, 2, 1)),
        ([], CountSummary("no-context", None, 0, 0)),
    ],
)
def test_summarize_count_results(rows, expected):
    assert summarize_count_results(rows) == expected


def test_summarize_count_results_rejects_non_count_rows_like_the_analyzer():
    with pytest.raises(ValueError, match="banana"):
        summarize_count_results(["banana"])


# --- dialect_fit ------------------------------------------------------------------

C, S = Dialect.CLIENT, Dialect.SESSION


@pytest.mark.parametrize(
    ("text", "dialects", "basis"),
    [
        ('names of files of folder "/etc"', {C}, "resolved"),
        ("names of bes computers", {S}, "resolved"),
        # Name-level resolution says "both" (operating system of <bes computer>
        # is a session property); only the client type check accepts it.
        ("name of operating system", {C}, "type-check"),
        ("version of operating system", {C}, "type-check"),
        ("now", {C, S}, "either"),
        ('(1, "a")', {C, S}, "either"),
        ("names of bes computers whose (name of it = computer name)", {C, S}, "mixed"),
        # A typo type-checks in neither: client, as when a typo breaks the parse...
        ("nme of operating system", {C}, "unclear"),
        # ...unless the text itself says which: "bes <word>" is session.
        ("names of bes computerz", {S}, "classified"),
        ("names of (bes computers", {S}, "classified"),
        ("name of (operating system", {C}, "default"),
    ],
)
def test_dialect_fit(text, dialects, basis):
    fit = dialect_fit(text)

    assert isinstance(fit, DialectFit)
    assert fit.dialects == frozenset(dialects)
    assert fit.basis == basis


def test_dialect_fit_names_what_each_side_contributes_when_mixed():
    fit = dialect_fit("names of bes computers whose (name of it = computer name)")

    assert fit.client_only == ("computer name",)
    assert fit.session_only == ("bes computers",)


def test_dialect_fit_names_session_only_references():
    assert dialect_fit("names of bes computers").session_only == ("bes computers",)


# --- span_leads -------------------------------------------------------------------


def unknown_finding(statement, dialect=Dialect.CLIENT):
    config = LintConfig(dialect=dialect, suggest=True)
    [finding] = [f for f in lint_text(statement, config) if f.code == "unknown-inspector"]
    return finding


def test_span_leads_are_per_written_name_in_span_order():
    statement = "nme of Operating  Sytem"

    leads = span_leads(statement, unknown_finding(statement), dialect=Dialect.CLIENT)

    assert leads == (
        suggest("nme", dialect=Dialect.CLIENT),
        suggest("operating sytem", dialect=Dialect.CLIENT),
    )
    assert leads[1][0] == "operating system"


def test_span_leads_repeat_for_each_use_of_a_name():
    statement = "nme of nme of operating system"

    first, second = span_leads(statement, unknown_finding(statement), dialect=Dialect.CLIENT)

    assert first == second == suggest("nme", dialect=Dialect.CLIENT)


@pytest.mark.parametrize(
    ("statement", "dialect"),
    [
        ("nme of Operating  Sytem", Dialect.CLIENT),
        ("nme of nme of operating system", Dialect.CLIENT),
        ("names of bes computerz", Dialect.SESSION),
    ],
)
def test_span_leads_together_are_the_findings_suggestions(statement, dialect):
    """The finding's own list is every span's leads, de-duplicated in order."""
    finding = unknown_finding(statement, dialect)

    leads = span_leads(statement, finding, dialect=dialect)

    assert tuple(dict.fromkeys(lead for each in leads for lead in each)) == finding.suggestions


def test_span_leads_are_empty_for_other_findings():
    statement = "(name of operating system) + 1"
    [finding] = lint_text(statement, LintConfig(dialect=Dialect.CLIENT, suggest=True))

    assert finding.code == "type-error"
    assert span_leads(statement, finding, dialect=Dialect.CLIENT) == ((),)
