"""Tests for upstream/besapi.py.

Written to move into besapi's own suite together with the module: only that
library and the stdlib are used. The fixtures are ``/api/query`` responses
recorded from a real root server (besapi 4.4.3), so they pin the server's
format, not a guess at it.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from bigfix_relevance_debugger.upstream.besapi import (
    SessionRelevanceResult,
    parse_query_result,
    session_relevance_result,
)

FIXTURES = Path(__file__).parent / "fixtures" / "besapi"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_singular_answer_with_type_time_and_plurality():
    result = parse_query_result("now", fixture("singular.xml"))

    assert isinstance(result, SessionRelevanceResult)
    assert result.ok
    assert result.relevance == "now"
    assert result.answers == ("Sat, 10 Oct 2026 22:13:25 -0400",)
    assert result.answer_types == ("time",)
    assert result.plural is False
    assert result.evaluation_time == "0.31ms"
    assert result.evaluation_ms == pytest.approx(0.31)
    assert result.error is None


def test_plural_answers_keep_their_text():
    result = parse_query_result("(1;2;3)", fixture("plural.xml"))

    assert result.answers == ("1", "2", "3")
    assert result.answer_types == ("integer", "integer", "integer")
    assert result.plural is True


def test_tuples_render_the_way_qna_shows_them():
    # besapi's session_relevance_array loses these entirely (it only reads
    # Result/Answer), which is why this module parses the XML itself.
    result = parse_query_result('((1, "a"); (2, "b"))', fixture("tuples.xml"))

    assert result.answers == ("1, a", "2, b")
    assert result.answer_types == ("( integer, string )", "( integer, string )")


def test_nested_tuple():
    xml = (
        "<BESAPI><Query><Result><Tuple><Answer type='integer'>1</Answer>"
        "<Tuple><Answer type='string'>a</Answer><Answer type='boolean'>True</Answer></Tuple>"
        "</Tuple></Result></Query></BESAPI>"
    )

    result = parse_query_result("x", xml)

    assert result.answers == ("1, a, True",)
    assert result.answer_types == ("( integer, ( string, boolean ) )",)


def test_empty_result_is_ok_with_no_answers():
    # besapi's session_relevance_array returns a "<Nothing> ..." sentinel here.
    result = parse_query_result("names of bes computers whose (false)", fixture("empty.xml"))

    assert result.ok
    assert result.answers == ()
    assert result.plural is True


def test_empty_string_answer_is_an_answer():
    xml = "<BESAPI><Query><Result><Answer type='string'/></Result></Query></BESAPI>"

    assert parse_query_result('""', xml).answers == ("",)


def test_relevance_error_carries_the_server_message():
    result = parse_query_result("nme of bes computers", fixture("error.xml"))

    assert not result.ok
    assert result.error == 'The operator "nme" is not defined.'
    assert result.answers == ()
    assert result.plural is None
    assert result.evaluation_time is None


@pytest.mark.parametrize(("text", "ms"), [("0.31ms", 0.31), ("2s", 2000.0), ("soon", None)])
def test_evaluation_ms_parses_the_units_it_knows(text, ms):
    xml = f"<BESAPI><Query><Result/><Evaluation><Time>{text}</Time></Evaluation></Query></BESAPI>"

    result = parse_query_result("x", xml)

    assert result.evaluation_time == text
    assert result.evaluation_ms == (pytest.approx(ms) if ms is not None else None)


def test_non_xml_response_raises_value_error():
    with pytest.raises(ValueError, match="not a BESAPI query result"):
        parse_query_result("x", "<html><body>Unauthorized</body></html>")


def test_unparseable_response_raises_value_error():
    with pytest.raises(ValueError, match="not a BESAPI query result"):
        parse_query_result("x", "Unauthorized")


class FakeConnection:
    def __init__(self, text: str, status_code: int = 200) -> None:
        self.text = text
        self.status_code = status_code
        self.calls: list[tuple[str, dict]] = []

    def session_relevance_xml(self, relevance, **kwargs):
        self.calls.append((relevance, kwargs))
        return SimpleNamespace(
            text=self.text, request=SimpleNamespace(status_code=self.status_code)
        )


def test_session_relevance_result_queries_and_parses():
    conn = FakeConnection(fixture("plural.xml"))

    result = session_relevance_result(conn, "(1;2;3)", timeout=(5, 30))

    assert result.answers == ("1", "2", "3")
    assert conn.calls == [("(1;2;3)", {"timeout": (5, 30)})]


def test_session_relevance_result_reports_http_status_for_a_non_result():
    conn = FakeConnection("<html>Unauthorized</html>", status_code=401)

    with pytest.raises(ValueError, match="HTTP 401"):
        session_relevance_result(conn, "now")
