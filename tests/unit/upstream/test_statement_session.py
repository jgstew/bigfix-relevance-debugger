"""Tests for upstream.relevance_analyzer.StatementSession.

Written to move into bigfix-relevance-analyzer's own suite together with the
class: only that library's public API and the stdlib are used. Every test
runs the real language server in-process; nothing touches a socket or a file.
"""

from __future__ import annotations

import pytest
from bigfix_relevance_analyzer import Dialect

from bigfix_relevance_debugger.upstream.relevance_analyzer import LspError, StatementSession

SETTING = 'exists values of setting "x" of client'


def at(line, character):
    return {"line": line, "character": character}


def span(start, end):
    return {"start": at(*start), "end": at(*end)}


@pytest.fixture
def session():
    s = StatementSession()
    yield s
    s.close()


# --- diagnostics ------------------------------------------------------------------


def test_first_sync_publishes_diagnostics(session):
    diagnostics = session.sync("nme of operating system")

    assert diagnostics is not None
    [diagnostic] = diagnostics
    assert diagnostic["code"] == "unknown-inspector"
    assert diagnostic["range"] == span((0, 0), (0, 3))
    assert session.version == 1


def test_unchanged_text_sends_nothing(session):
    session.sync("name of operating system")

    assert session.sync("name of operating system") is None
    assert session.version == 1


def test_changed_text_publishes_again(session):
    session.sync("nme of operating system")

    assert session.sync("name of operating system") == []
    assert session.version == 2


def test_clean_text_has_no_diagnostics(session):
    assert session.sync("name of operating system") == []


# --- requests ---------------------------------------------------------------------


def test_completion_after_of(session):
    result = session.request("textDocument/completion", {"position": at(0, 8)}, "name of ")

    assert "operating system" in [item["label"] for item in result["items"]]


def test_a_request_syncs_its_text_first(session):
    session.sync("nme")

    result = session.request("textDocument/completion", {"position": at(1, 8)}, "\nname of ")

    assert session.version == 2
    assert "operating system" in [item["label"] for item in result["items"]]


def test_hover_describes_the_inspector(session):
    result = session.request(
        "textDocument/hover", {"position": at(0, 12)}, "name of operating system"
    )

    assert "operating system" in result["contents"]["value"]
    assert result["range"] == span((0, 8), (0, 24))


def test_hover_on_nothing_is_none(session):
    assert session.request("textDocument/hover", {"position": at(0, 4)}, "name of x") is None


def test_quick_fix_edits_the_statement(session):
    actions = session.request(
        "textDocument/codeAction",
        {"range": span((0, 20), (0, 20)), "context": {"diagnostics": []}},
        SETTING,
    )

    [quick] = [a for a in actions if a["kind"] == "quickfix"]
    assert quick["title"] == "Change `setting` to `settings`"
    # A minimal edit: `setting` becomes `settings` by inserting the `s`.
    [edit] = quick["edit"]["changes"][StatementSession.URI]
    assert edit == {"range": span((0, 24), (0, 24)), "newText": "s"}


def test_fix_all_is_offered(session):
    actions = session.request(
        "textDocument/codeAction",
        {
            "range": span((0, 0), (0, 0)),
            "context": {"diagnostics": [], "only": ["source.fixAll.bigfix-relevance"]},
        },
        SETTING,
    )

    assert [a["kind"] for a in actions] == ["source.fixAll.bigfix-relevance"]


def test_an_error_response_raises(session):
    with pytest.raises(LspError) as raised:
        session.request("textDocument/nonsense", {}, "now")

    assert raised.value.code == -32601
    assert "textDocument/nonsense" in str(raised.value)


# --- dialect ----------------------------------------------------------------------


def hover_text(session, text, character):
    return session.request("textDocument/hover", {"position": at(0, character)}, text)["contents"][
        "value"
    ]


def test_dialect_defaults_to_whatever_the_statement_says():
    # `computer` alone is a client inspector (macOS) and a session one; an
    # undecided statement is analysed as client relevance.
    session = StatementSession()

    assert "session" not in hover_text(session, "name of computer", 10)


def test_a_forced_dialect_reaches_the_server():
    session = StatementSession(dialect=Dialect.SESSION)

    assert session.dialect is Dialect.SESSION
    assert "session" in hover_text(session, "name of computer", 10)


# --- lifecycle --------------------------------------------------------------------


def test_closed_session_refuses_work():
    session = StatementSession()
    session.sync("now")

    session.close()
    session.close()  # idempotent

    with pytest.raises(RuntimeError, match="closed"):
        session.sync("now")


# --- the latest diagnostics -------------------------------------------------------


def test_diagnostics_start_empty(session):
    assert session.diagnostics == []


def test_diagnostics_are_kept_for_the_text_the_server_holds(session):
    session.sync("nme of operating system")

    assert session.sync("nme of operating system") is None  # nothing new to report...
    [diagnostic] = session.diagnostics  # ...but the latest are still at hand
    assert diagnostic["code"] == "unknown-inspector"


def test_a_requests_sync_keeps_its_diagnostics(session):
    # request() syncs first; a caller that publishes afterwards must still be
    # able to get that text's diagnostics.
    session.request("textDocument/hover", {"position": at(0, 1)}, "nme of operating system")

    assert [d["code"] for d in session.diagnostics] == ["unknown-inspector"]
