"""Tests for upstream/besapi.py.

Written to move into besapi's own suite together with the module: only that
library and the stdlib are used. The fixtures are ``/api/query`` responses
recorded from a real root server (besapi 4.4.3), so they pin the server's
format, not a guess at it.
"""

from __future__ import annotations

import gc
import json
import logging
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, quote, unquote
from xml.sax.saxutils import escape as xml_escape

import pytest

from bigfix_relevance_debugger.upstream import besapi as besapi_upstream
from bigfix_relevance_debugger.upstream.besapi import (
    SessionRelevanceResult,
    parse_query_result,
    post_session_relevance,
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


class QueryServer:
    """A fake ``BESConnection`` whose ``/api/query`` decodes like a root server.

    ``state`` is who answers ``/api/query`` (jgstew/besapi#30): ``"explorer"``
    form-decodes the body once; ``"web_reports"`` (no reachable Explorer)
    form-decodes it and then percent-decodes it again, keeping ``+``. A query
    answers the text the server would evaluate, so tests compare it with the
    relevance. The ``application/json+relevance`` probe gets 200 from Explorer
    and 503 from Web Reports, unless ``probe_status`` overrides it. A query
    whose evaluated text is valid JSON but not an object gets the live root
    server's 400 (seen with Explorer answering, 2026-10-18). Like
    ``BESConnection`` it defines ``__eq__`` and so is unhashable.
    """

    __hash__ = None  # type: ignore[assignment]

    def __eq__(self, other: object) -> bool:
        return isinstance(other, QueryServer) and other.rootserver == self.rootserver

    def __init__(
        self, state: str, probe_status: int | None = None, probe_body: str | None = None
    ) -> None:
        self.state = state
        self.probe_status = probe_status
        self.probe_body = probe_body
        self.rootserver = "https://root.example:52311"
        self.posts: list[tuple[str, object, dict]] = []

    @property
    def probes(self) -> list[dict]:
        return [kw for _, _, kw in self.posts if _is_probe(kw)]

    @property
    def queries(self) -> list[object]:
        return [data for _, data, kw in self.posts if not _is_probe(kw)]

    def post(self, path, data, **kwargs):
        self.posts.append((path, data, kwargs))
        assert path == "query"
        if _is_probe(kwargs):
            assert json.loads(data) == {"relevance": "1"}
            status = self.probe_status or (200 if self.state == "explorer" else 503)
            if self.probe_body is not None:
                return _response(self.probe_body, status)
            return _response(EXPLORER_PROBE_ANSWER if status == 200 else "unavailable", status)
        evaluated = self.evaluated(data)
        if _is_json_but_not_an_object(evaluated):
            return _response(JSON_RELEVANCE_ANSWER, 400)
        return _response(self._answer(evaluated))

    def evaluated(self, data) -> str:
        if isinstance(data, dict):
            # requests form-encodes a dict, and the server form-decodes it.
            value = data["relevance"]
        else:
            (value,) = parse_qs(data, keep_blank_values=True)["relevance"]
        return unquote(value) if self.state == "web_reports" else value

    @staticmethod
    def _answer(text: str) -> str:
        return (
            "<BESAPI><Query><Result>"
            f"<Answer type='string'>{xml_escape(text)}</Answer>"
            "</Result></Query></BESAPI>"
        )


# Explorer's answer to the probe, recorded from a live root server (2026-10-18).
EXPLORER_PROBE_ANSWER = (
    '{"result":[1],"count":1,"plural":false,"type":"integer","evaltime_ms":0.04}'
)


# The live root server's answer to a form-posted relevance of "now" (with
# the quotes), 2026-10-18.
JSON_RELEVANCE_ANSWER = (
    '{"error":"Invalid JSON: Invalid required value type, expected type: \\"object\\""}'
)


def _is_json_but_not_an_object(text: str) -> bool:
    try:
        value = json.loads(text)
    except ValueError:
        return False
    return not isinstance(value, dict)


def _is_probe(kwargs: dict) -> bool:
    return kwargs.get("headers", {}).get("Content-Type") == "application/json+relevance"


def _response(text: str, status: int = 200) -> SimpleNamespace:
    return SimpleNamespace(text=text, request=SimpleNamespace(status_code=status))


STATES = ["explorer", "web_reports"]

# jgstew/besapi#30's corpora. NO_PERCENT evaluates the same in both states
# when sent encoded once; PERCENT depends on who answers.
NO_PERCENT = [
    "now",
    "number of bes sites",
    '(1, "a")',
    "1 + 1",
    '("a+b")',
    '("a&b=c")',
    '("a b")',
    '("é")',
    'names of bes sites whose (name of it contains "Master")',
    '((1, "a"); (2, "b"))',
]
PERCENT = [
    '("a%22b")',
    '"100%25 of " & "5+5"',
    '("x%22y")',
    '("100%25 done")',
    '("%2541")',
    '(it as string) of ( "a%22b" )',
]


@pytest.fixture(autouse=True)
def _fresh_probe_cache():
    besapi_upstream.forget_query_encoding()
    yield
    besapi_upstream.forget_query_encoding()


# --- Regression: relevance without % sends besapi 4.2.0's request ---------


@pytest.mark.parametrize("state", STATES)
@pytest.mark.parametrize("relevance", NO_PERCENT)
def test_no_percent_sends_the_besapi_420_body_and_no_probe(state, relevance):
    conn = QueryServer(state)

    post_session_relevance(conn, relevance)

    assert conn.posts == [("query", "relevance=" + quote(relevance, safe=":"), {})]


@pytest.mark.parametrize("state", STATES)
@pytest.mark.parametrize("relevance", NO_PERCENT)
def test_no_percent_evaluates_the_relevance_in_both_states(state, relevance):
    conn = QueryServer(state)

    result = session_relevance_result(conn, relevance)

    assert result.answers == (relevance,)


def test_no_percent_never_probes_across_many_queries():
    conn = QueryServer("explorer")

    for relevance in NO_PERCENT * 3:
        session_relevance_result(conn, relevance)

    assert conn.probes == []
    assert len(conn.queries) == len(NO_PERCENT) * 3


# --- Regression: % without Explorer keeps besapi 4.3.1+'s double encoding --


@pytest.mark.parametrize("relevance", PERCENT)
def test_percent_under_web_reports_sends_the_besapi_431_body(relevance):
    conn = QueryServer("web_reports")

    result = session_relevance_result(conn, relevance)

    assert conn.queries == [{"relevance": quote(relevance, safe=":+")}]
    assert result.answers == (relevance,)


def test_percent_escape_is_not_decoded_early_under_web_reports():
    # Encoded once, Web Reports would evaluate ("%41"), i.e. "A": a silent
    # wrong answer. This is why an unknown state means "encode twice".
    conn = QueryServer("web_reports")

    assert session_relevance_result(conn, '("%2541")').answers == ('("%2541")',)


# --- New: % with Explorer is encoded once ---------------------------------


@pytest.mark.parametrize("state", STATES)
@pytest.mark.parametrize("relevance", PERCENT)
def test_percent_evaluates_the_relevance_in_both_states(state, relevance):
    conn = QueryServer(state)

    result = session_relevance_result(conn, relevance)

    assert result.answers == (relevance,)
    assert len(conn.probes) == 1


@pytest.mark.parametrize("relevance", PERCENT)
def test_percent_under_explorer_sends_the_body_encoded_once(relevance):
    conn = QueryServer("explorer")

    post_session_relevance(conn, relevance)

    assert conn.queries == ["relevance=" + quote(relevance, safe=":")]


@pytest.mark.parametrize("status", [400, 401, 404, 500])
def test_any_probe_answer_but_200_means_encode_twice(status):
    conn = QueryServer("web_reports", probe_status=status)

    result = session_relevance_result(conn, '("x%22y")')

    assert result.answers == ('("x%22y")',)
    assert conn.queries == [{"relevance": quote('("x%22y")', safe=":+")}]


def test_a_probe_that_raises_means_encode_twice(caplog):
    conn = QueryServer("web_reports")
    real_post = conn.post

    def post(path, data, **kwargs):
        if _is_probe(kwargs):
            conn.posts.append((path, data, kwargs))
            raise ConnectionError("no route")
        return real_post(path, data, **kwargs)

    conn.post = post  # type: ignore[method-assign]

    with caplog.at_level(logging.DEBUG, logger=besapi_upstream.__name__):
        result = session_relevance_result(conn, '("x%22y")')

    assert result.answers == ('("x%22y")',)
    assert "probe" in caplog.text


@pytest.mark.parametrize(
    "body", ["<html>Sign in</html>", '{"result":[]}', '{"error":"no"}', "[1]", ""]
)
def test_a_200_probe_without_explorers_answer_means_encode_twice(body):
    # e.g. a proxy's login page: encoding once would let Web Reports decode
    # ("%2541") into ("%41") without any error.
    conn = QueryServer("web_reports", probe_status=200, probe_body=body)

    result = session_relevance_result(conn, '("%2541")')

    assert result.answers == ('("%2541")',)
    assert conn.queries == [{"relevance": quote('("%2541")', safe=":+")}]


def test_a_probe_that_raises_is_not_cached():
    conn = QueryServer("explorer")
    real_post = conn.post
    fail = [True]

    def post(path, data, **kwargs):
        if _is_probe(kwargs) and fail[0]:
            conn.posts.append((path, data, kwargs))
            fail[0] = False
            raise TimeoutError("read timed out")
        return real_post(path, data, **kwargs)

    conn.post = post  # type: ignore[method-assign]

    session_relevance_result(conn, '("x%22y")')
    result = session_relevance_result(conn, '("x%22y")')

    assert len(conn.probes) == 2
    assert result.answers == ('("x%22y")',)


def test_an_http_error_from_raise_for_status_is_cached():
    # BESConnection(raise_for_status=True) raises requests.HTTPError for the
    # 503: that is the server's answer, not a network blip.
    conn = QueryServer("web_reports")
    real_post = conn.post

    def post(path, data, **kwargs):
        response = real_post(path, data, **kwargs)
        if response.request.status_code >= 400:
            error = OSError("503 Server Error")
            error.response = response.request  # type: ignore[attr-defined]
            raise error
        return response

    conn.post = post  # type: ignore[method-assign]

    session_relevance_result(conn, '("x%22y")')
    result = session_relevance_result(conn, '("x%22y")')

    assert len(conn.probes) == 1
    assert result.answers == ('("x%22y")',)


def test_a_connection_without_weakref_support_still_works():
    class SlotConnection:
        __slots__ = ("server",)

        def __init__(self) -> None:
            self.server = QueryServer("explorer")

        @property
        def rootserver(self) -> str:
            return self.server.rootserver

        def post(self, path, data, **kwargs):
            return self.server.post(path, data, **kwargs)

    conn = SlotConnection()

    assert session_relevance_result(conn, '("x%22y")').answers == ('("x%22y")',)
    assert session_relevance_result(conn, '("x%22y")').answers == ('("x%22y")',)
    assert len(conn.server.probes) == 2  # not cached: no way to drop the entry later


def test_probe_is_cached_per_connection(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(besapi_upstream, "_monotonic", lambda: now[0])
    conn, other = QueryServer("explorer"), QueryServer("web_reports")

    for relevance in PERCENT:
        session_relevance_result(conn, relevance)
    session_relevance_result(other, '("x%22y")')

    assert len(conn.probes) == 1
    assert len(other.probes) == 1
    assert other.queries == [{"relevance": quote('("x%22y")', safe=":+")}]


def test_probe_cache_entry_goes_with_its_connection():
    conn = QueryServer("explorer")
    session_relevance_result(conn, '("x%22y")')
    assert len(besapi_upstream._probed) == 1

    del conn
    gc.collect()

    assert besapi_upstream._probed == {}


def test_probe_is_repeated_after_the_recheck_interval(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(besapi_upstream, "_monotonic", lambda: now[0])
    conn = QueryServer("explorer")

    session_relevance_result(conn, '("x%22y")')
    now[0] += besapi_upstream.EXPLORER_RECHECK_SECONDS - 1
    session_relevance_result(conn, '("x%22y")')
    assert len(conn.probes) == 1

    conn.state = "web_reports"  # Explorer stopped
    now[0] += 2
    result = session_relevance_result(conn, '("x%22y")')

    assert len(conn.probes) == 2
    assert result.answers == ('("x%22y")',)


def test_probe_cache_follows_a_rootserver_change():
    conn = QueryServer("explorer")
    session_relevance_result(conn, '("x%22y")')

    conn.rootserver = "https://other.example:52311"
    conn.state = "web_reports"
    result = session_relevance_result(conn, '("x%22y")')

    assert len(conn.probes) == 2
    assert result.answers == ('("x%22y")',)


@pytest.mark.parametrize("relevance", ["now", '("x%22y")'])
def test_kwargs_reach_the_query_and_the_probe(relevance):
    conn = QueryServer("explorer")

    session_relevance_result(conn, relevance, timeout=(5, 30))

    assert all(kw.get("timeout") == (5, 30) for _, _, kw in conn.posts)


def test_caller_headers_are_kept_on_the_probe():
    conn = QueryServer("explorer")

    post_session_relevance(conn, '("x%22y")', headers={"X-Trace": "1"})

    (probe,) = conn.probes
    assert probe["headers"] == {"X-Trace": "1", "Content-Type": "application/json+relevance"}


# --- session_relevance_result ---------------------------------------------


def test_session_relevance_result_queries_and_parses():
    conn = QueryServer("explorer")
    conn._answer = lambda _text: fixture("plural.xml")  # type: ignore[method-assign]

    result = session_relevance_result(conn, "(1;2;3)", timeout=(5, 30))

    assert result.answers == ("1", "2", "3")
    assert conn.posts == [("query", "relevance=%281%3B2%3B3%29", {"timeout": (5, 30)})]


def test_session_relevance_result_reports_http_status_for_a_non_result():
    conn = QueryServer("explorer")
    conn.post = lambda *a, **kw: _response("<html>Unauthorized</html>", 401)  # type: ignore[method-assign]

    with pytest.raises(ValueError, match="HTTP 401"):
        session_relevance_result(conn, "now")


# --- New: relevance that is valid JSON is sent in parentheses --------------

# Each got HTTP 400 "Invalid JSON ... expected type: "object"" from a live root
# server when sent as is, and its answer when sent in parentheses (2026-10-18).
JSON_SHAPED = [
    '"now"',
    '"this has a space"',
    '"a%2541"',
    "1",
    "-1",
    "1.5",
    "true",
    "null",
    ' "x"\n',
]


@pytest.mark.parametrize("state", STATES)
@pytest.mark.parametrize("relevance", JSON_SHAPED)
def test_json_shaped_relevance_is_evaluated_in_parentheses(state, relevance):
    conn = QueryServer(state)

    result = session_relevance_result(conn, relevance)

    assert result.ok
    assert result.relevance == relevance
    assert result.answers == (f"({relevance})",)


@pytest.mark.parametrize("relevance", ['"a" & "b"', "now", '("now")', 'it of "x"'])
def test_relevance_that_is_not_json_is_sent_as_is(relevance):
    conn = QueryServer("explorer")

    assert session_relevance_result(conn, relevance).answers == (relevance,)
