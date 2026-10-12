"""Tests for analysis.py: analyzer output -> plain view-model the UI renders.

Everything here runs the real analyzer (it is pure and offline), so these
tests also pin the slice of its behaviour the UI depends on.
"""

from __future__ import annotations

import dataclasses

import pytest

from bigfix_relevance_debugger.analysis import (
    AnalysisView,
    AutofixView,
    SpanView,
    SuggestionFix,
    editor_decorations,
    missing_fix_reason,
    suggestion_fix,
    summarize,
)

WHOSE_EXAMPLE = 'names of files whose (size of it > 1000) of folder "/etc"'
# The analyzer's own autofix example: `setting` mid-chain should be plural.
SETTING = 'exists values of setting "x" of client'


def test_valid_expression_has_no_parse_error_and_can_break_down():
    view = summarize("name of operating system")

    assert isinstance(view, AnalysisView)
    assert view.parse_error is None
    assert view.can_breakdown is True
    assert view.breakdown_disabled_reason is None
    assert view.findings == ()


def test_valid_expression_type_summary():
    view = summarize("name of operating system")

    assert view.type_summary == "singular string"
    assert view.type_errors == ()


def test_valid_expression_complexity():
    view = summarize("name of operating system")

    assert view.complexity_score > 0
    assert view.evaluation_cost == 0
    assert view.costly_inspectors == ()


def test_costly_inspectors_surface():
    view = summarize('names of files of descendant folders of folder "/"')

    assert "folder recursion" in view.costly_inspectors
    assert view.evaluation_cost > 0


@pytest.mark.parametrize(
    ("text", "run", "dialect"),
    [
        ("name of operating system", ("client",), "client"),
        ("names of bes computers", ("session",), "session"),
        # Session first: the issue asks for session results to be listed first.
        ("now", ("session", "client"), "session + client"),
        (
            "names of bes computers whose (name of it = computer name)",
            ("session", "client"),
            "session + client",
        ),
        # A typo: client, not a query to the root server as well.
        ("name of operating sytem", ("client",), "client"),
    ],
)
def test_auto_dialect_decides_what_runs(text, run, dialect):
    view = summarize(text)

    assert view.dialect_mode == "auto"
    assert view.run_dialects == run
    assert view.dialect == dialect


@pytest.mark.parametrize(
    ("text", "words"),
    [
        ("name of operating system", "only type-checks as client"),
        ("names of bes computers", "bes computers"),
        ("now", "either"),
        ("names of bes computers whose (name of it = computer name)", "computer name"),
        ("name of (operating system", "assuming client"),
        ("name of operating sytem", "neither dialect; assuming client"),
        ("names of (bes computers", "looks like session"),
    ],
)
def test_auto_dialect_says_why(text, words):
    assert words in summarize(text).dialect_reason


@pytest.mark.parametrize(
    ("mode", "run"),
    [("client", ("client",)), ("session", ("session",)), ("both", ("session", "client"))],
)
def test_dialect_override_narrows_or_widens_the_run(mode, run):
    view = summarize("now", mode)

    assert view.dialect_mode == mode
    assert view.run_dialects == run
    assert "set to" in view.dialect_reason


@pytest.mark.parametrize(
    ("text", "mode", "optional"),
    [
        # Auto added session only because the expression is valid in either.
        ("now", "auto", True),
        ("names of bes computers whose (name of it = computer name)", "auto", True),
        # Session is the point of these runs.
        ("names of bes computers", "auto", False),
        ("now", "both", False),
        ("now", "session", False),
        # No session at all.
        ("name of operating system", "auto", False),
        ("now", "client", False),
    ],
)
def test_session_is_optional_only_when_auto_added_it_beside_client(text, mode, optional):
    assert summarize(text, mode).session_optional is optional


def test_unknown_dialect_mode_is_rejected():
    with pytest.raises(ValueError, match="dialect"):
        summarize("now", "sometimes")


def test_session_only_expression_is_analyzed_and_linted_as_session():
    view = summarize("names of bes computers")

    # As client relevance it is unknown; as session relevance it type-checks.
    assert view.type_summary == "plural string"
    assert view.findings == ()


def test_session_only_expression_cannot_break_down_yet():
    view = summarize("names of bes computers")

    assert view.can_breakdown is False
    assert view.breakdown_disabled_reason is not None
    assert "session" in view.breakdown_disabled_reason.lower()


def test_expression_run_both_ways_breaks_down_as_client():
    view = summarize("now")

    assert view.can_breakdown is True
    assert view.levels


def test_parse_error_reports_line_and_column():
    view = summarize("name of (operating system")

    assert view.parse_error is not None
    assert view.parse_error.line == 1
    assert view.parse_error.column == 26
    assert view.parse_error.offset == 25
    assert "expected ')'" in view.parse_error.message


def test_parse_error_disables_breakdown_with_a_reason():
    view = summarize("name of (operating system")

    assert view.can_breakdown is False
    assert view.levels == ()
    assert view.breakdown_disabled_reason is not None
    assert "parse" in view.breakdown_disabled_reason.lower()


def test_parse_error_has_no_type_summary():
    assert summarize("name of (operating system").type_summary is None


def test_empty_text_cannot_break_down():
    view = summarize("   ")

    assert view.parse_error is not None
    assert view.can_breakdown is False


def test_lint_finding_with_suggestions():
    view = summarize("name of operating sytem")

    assert view.parse_error is None
    [finding] = view.findings
    assert finding.code == "unknown-inspector"
    assert finding.severity == "warning"
    assert finding.line == 1
    assert "operating system" in [f.suggestion for f in finding.fixes]


def test_type_error_surfaces_as_type_error_and_finding():
    view = summarize("(name of operating system) + 1")

    assert view.type_errors
    assert "'+'" in view.type_errors[0]
    assert [f.code for f in view.findings] == ["type-error"]
    assert view.findings[0].severity == "error"


def test_finding_spans_are_plain_offsets():
    view = summarize("(name of operating system) + 1")

    assert [(s.start, s.end) for s in view.findings[0].spans] == [(0, 30)]


@pytest.mark.parametrize("lead", ["  ", "\n", " \n\t"])
def test_finding_spans_index_the_text_as_typed(lead):
    # lint_text analyses text.strip(), so its spans start after any leading
    # whitespace; the view's spans must index what the editor shows.
    text = lead + "name of operating sytem"

    [span] = summarize(text).findings[0].spans

    assert text[span.start : span.end] == "operating sytem"


def test_unknown_name_spans_carry_their_own_message_and_leads():
    view = summarize("nme of operating sytem")

    first, second = view.findings[0].spans
    assert isinstance(first, SpanView)
    assert "`nme`" in (first.message or "") and "operating sytem" not in (first.message or "")
    assert "`operating sytem`" in (second.message or "")
    assert second.suggestions[0] == "operating system"
    assert "operating system" not in first.suggestions


def test_other_spans_have_no_leads():
    [span] = summarize("(name of operating system) + 1").findings[0].spans

    assert span.suggestions == ()


# --- fixes -------------------------------------------------------------------


def test_each_lead_is_a_fix_that_replaces_the_name():
    view = summarize("  name of operating sytem\n")

    fix = view.findings[0].fixes[0]
    assert fix == SuggestionFix(
        name="operating sytem",
        suggestion="operating system",
        label="operating system",
        spans=((10, 25),),
    )
    assert [f.suggestion for f in view.findings[0].fixes] == list(
        view.findings[0].spans[0].suggestions
    )
    assert (
        suggestion_fix(view, "operating sytem", "operating system")
        == "  name of operating system\n"
    )


def test_a_fix_replaces_every_use_of_the_name():
    view = summarize("nme of NME of operating system")

    [finding] = view.findings
    fix = finding.fixes[0]
    assert fix.name == "nme"
    assert len(fix.spans) == 2
    assert len(finding.fixes) == len(finding.spans[0].suggestions)  # one per lead, not per use
    assert (
        suggestion_fix(view, "nme", fix.suggestion)
        == f"{fix.suggestion} of {fix.suggestion} of operating system"
    )


def test_fixes_are_per_name_and_say_which_name_when_there_are_several():
    view = summarize("nme of operating sytem")

    fixes = view.findings[0].fixes
    assert {f.name for f in fixes} == {"nme", "operating sytem"}
    fix = next(f for f in fixes if f.suggestion == "operating system")
    assert fix.label == "operating sytem \u2192 operating system"
    assert suggestion_fix(view, "operating sytem", "operating system") == "nme of operating system"


def test_findings_without_leads_have_no_fixes():
    assert summarize("(name of operating system) + 1").findings[0].fixes == ()


def test_no_fix_once_the_name_is_gone():
    view = summarize("name of operating system")

    assert suggestion_fix(view, "operating sytem", "operating system") is None
    assert missing_fix_reason(view, "operating sytem", "operating system") == (
        "operating sytem is no longer in the text."
    )


def test_no_fix_when_the_name_is_still_there_but_not_that_lead():
    # The session dialect has other leads for `filez` than client's `file`.
    view = summarize('names of filez of folder "/etc"', "session")

    assert suggestion_fix(view, "filez", "file") is None
    assert missing_fix_reason(view, "filez", "file") == ("file is no longer suggested for filez.")


def test_autofix_keeps_the_surrounding_whitespace():
    view = summarize("  " + SETTING + "\n")

    assert view.autofix == AutofixView(
        text='  exists values of settings "x" of client\n', applied=1
    )


def test_nothing_to_autofix():
    assert summarize("name of operating system").autofix is None
    assert summarize("nme of operating system").autofix is None
    assert summarize("name of (operating system").autofix is None


def test_levels_in_analyzer_order_with_whole_expression_first():
    view = summarize(WHOSE_EXAMPLE)

    assert [lv.index for lv in view.levels] == list(range(len(view.levels)))
    assert view.levels[0].label == WHOSE_EXAMPLE
    assert view.levels[0].is_whole_expression is True
    assert all(not lv.is_whole_expression for lv in view.levels[1:])


def test_whose_level_carries_unfiltered_probe():
    view = summarize(WHOSE_EXAMPLE)

    whose = [lv for lv in view.levels if lv.unfiltered_probe is not None]
    assert len(whose) == 1
    [level] = whose
    assert level.label == 'files whose (size of it > 1000) of folder "/etc"'
    assert level.unfiltered_probe is not None
    assert "files of it" in level.unfiltered_probe
    assert level.probe != level.unfiltered_probe


def test_level_span_offsets_index_into_text():
    view = summarize(WHOSE_EXAMPLE)

    for level in view.levels:
        assert WHOSE_EXAMPLE[level.start : level.end] == level.label


def test_level_span_line_and_column():
    level = next(lv for lv in summarize(WHOSE_EXAMPLE).levels if lv.label == "size of it")

    assert (level.line, level.column) == (1, 23)


def test_view_is_frozen():
    view = summarize("name of operating system")

    with pytest.raises(dataclasses.FrozenInstanceError):
        view.text = "changed"  # type: ignore[misc]


def test_never_raises_on_garbage():
    view = summarize('))) whose of "unterminated')

    assert view.parse_error is not None


# --- editor_decorations ------------------------------------------------------


def test_clean_text_has_no_decorations():
    assert editor_decorations(summarize("name of operating system")) == []


def mark(start, end, css_class, title):
    return {
        "kind": "mark",
        "from": start,
        "to": end,
        "class": css_class,
        "attributes": {"title": title},
    }


def test_parse_error_marks_the_offending_character():
    view = summarize("name of ) x")
    assert view.parse_error is not None

    assert editor_decorations(view) == [mark(8, 9, "bfrd-parse-error", view.parse_error.message)]


def test_parse_error_at_end_of_text_marks_the_last_character():
    # A mark cannot be empty, and offset == len(text) has nothing after it.
    view = summarize("name of (operating system")
    assert view.parse_error is not None

    assert editor_decorations(view) == [mark(24, 25, "bfrd-parse-error", view.parse_error.message)]


def test_empty_text_has_nothing_to_mark():
    assert editor_decorations(summarize("")) == []


def test_lint_findings_are_marked_by_severity_with_the_span_message():
    view = summarize("name of operating sytem")
    [span] = view.findings[0].spans
    assert span.message

    assert editor_decorations(view) == [mark(8, 23, "bfrd-warning", span.message)]


def test_a_span_without_its_own_message_shows_the_findings():
    view = summarize("(name of operating system) + 1")
    [finding] = view.findings
    assert finding.spans[0].message is None

    assert editor_decorations(view) == [mark(0, 30, "bfrd-error", finding.message)]


def test_marks_follow_leading_whitespace():
    [decoration] = editor_decorations(summarize("\n  name of operating sytem"))

    assert (decoration["from"], decoration["to"]) == (11, 26)


def test_highlighted_level_span_is_marked():
    view = summarize(WHOSE_EXAMPLE)
    level = view.levels[2]

    decorations = editor_decorations(view, highlight=(level.start, level.end))

    assert {"kind": "mark", "from": 22, "to": 32, "class": "bfrd-level"} in decorations


def test_out_of_range_or_empty_highlight_is_dropped():
    view = summarize("name of operating system")

    assert editor_decorations(view, highlight=(5, 5)) == []
    assert editor_decorations(view, highlight=(0, 999)) == []


# --- platforms ----------------------------------------------------------------


def test_platform_specific_expression_reports_its_platform():
    assert summarize('exists key "HKLM\\\\Software" of registry').platforms == ("windows",)


def test_portable_expression_reports_every_client_platform():
    assert summarize("name of operating system").platforms == (
        "debian",
        "macos",
        "rhel",
        "ubuntu",
        "windows",
    )


def test_platforms_exclude_session_environments():
    platforms = summarize('exists debianpackage "bash"').platforms

    assert platforms
    assert not any(p.startswith("session:") for p in platforms)


def test_unparseable_expression_has_no_platforms():
    assert summarize("name of (operating system").platforms == ()


def test_levels_after_the_whole_expression_are_innermost_first():
    # run_breakdown flags the first failing level after index 0 as the root
    # cause; that is only right while the analyzer emits post-order
    # (an object before the level that consumes it).
    labels = [lv.label for lv in summarize(WHOSE_EXAMPLE).levels]

    assert labels == [
        WHOSE_EXAMPLE,
        'files whose (size of it > 1000) of folder "/etc"',
        "size of it",
        WHOSE_EXAMPLE,
    ]
