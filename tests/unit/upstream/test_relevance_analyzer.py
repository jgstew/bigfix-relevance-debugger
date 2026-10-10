"""Tests for upstream/relevance_analyzer.py.

Written to move into bigfix-relevance-analyzer's own suite together with the
module: only that library's public API and the stdlib are used.
"""

from __future__ import annotations

import pytest
from bigfix_relevance_analyzer import Dialect, analyze_relevance

from bigfix_relevance_debugger.upstream.relevance_analyzer import (
    CountSummary,
    client_platforms,
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
