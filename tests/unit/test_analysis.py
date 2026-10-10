"""Tests for analysis.py: analyzer output -> plain view-model the UI renders.

Everything here runs the real analyzer (it is pure and offline), so these
tests also pin the slice of its behaviour the UI depends on.
"""

from __future__ import annotations

import dataclasses

import pytest

from bigfix_relevance_debugger.analysis import AnalysisView, editor_decorations, summarize

WHOSE_EXAMPLE = 'names of files whose (size of it > 1000) of folder "/etc"'


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


def test_client_neutral_expression_reports_neutral_dialect():
    # Nothing in it is specific to either dialect, so the classifier has no opinion.
    assert summarize("name of operating system").dialect == "neutral"


def test_session_only_expression_reports_session_dialect():
    assert summarize("names of bes computers").dialect == "session"


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
    assert "operating system" in finding.suggestions


def test_type_error_surfaces_as_type_error_and_finding():
    view = summarize("(name of operating system) + 1")

    assert view.type_errors
    assert "'+'" in view.type_errors[0]
    assert [f.code for f in view.findings] == ["type-error"]
    assert view.findings[0].severity == "error"


def test_finding_spans_are_plain_offsets():
    view = summarize("(name of operating system) + 1")

    assert view.findings[0].spans == ((0, 30),)


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


def test_parse_error_marks_the_offending_character():
    view = summarize("name of ) x")

    assert editor_decorations(view) == [
        {"kind": "mark", "from": 8, "to": 9, "class": "bfrd-parse-error"}
    ]


def test_parse_error_at_end_of_text_marks_the_last_character():
    # A mark cannot be empty, and offset == len(text) has nothing after it.
    view = summarize("name of (operating system")

    assert editor_decorations(view) == [
        {"kind": "mark", "from": 24, "to": 25, "class": "bfrd-parse-error"}
    ]


def test_empty_text_has_nothing_to_mark():
    assert editor_decorations(summarize("")) == []


def test_lint_findings_are_marked_by_severity():
    view = summarize("name of operating sytem")

    assert editor_decorations(view) == [
        {"kind": "mark", "from": 8, "to": 23, "class": "bfrd-warning"}
    ]


def test_highlighted_level_span_is_marked():
    view = summarize(WHOSE_EXAMPLE)
    level = view.levels[2]

    decorations = editor_decorations(view, highlight=(level.start, level.end))

    assert {"kind": "mark", "from": 22, "to": 32, "class": "bfrd-level"} in decorations


def test_out_of_range_or_empty_highlight_is_dropped():
    view = summarize("name of operating system")

    assert editor_decorations(view, highlight=(5, 5)) == []
    assert editor_decorations(view, highlight=(0, 999)) == []
