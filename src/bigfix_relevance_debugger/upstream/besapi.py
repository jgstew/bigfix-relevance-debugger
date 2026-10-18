"""Candidates for besapi.

See ``upstream/__init__.py`` for the rules.

``BESConnection.session_relevance_array`` reads only ``Result/Answer`` text:
it returns a ``"<Nothing> ..."`` sentinel for tuple answers and for an empty
result, an ``"ERROR: ..."`` string for a relevance error, and drops the answer
types, evaluation time and plurality the server sends. The JSON output rounds
the evaluation time to whole milliseconds and re-types the answers. So this
parses the ``/api/query`` XML instead.

It also builds the ``/api/query`` request itself (``post_session_relevance``)
instead of calling ``session_relevance_xml``, which encodes the relevance twice
since besapi 4.3.1. That is right when Web Reports answers ``/api/query`` (it
decodes twice) but breaks every relevance with a space when BigFix Explorer
answers (it decodes once). See jgstew/besapi#30; delete this half once besapi
ships that fix.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import urllib.parse
import weakref
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Any

_logger = logging.getLogger(__name__)

_TIME_RE = re.compile(r"^\s*([0-9]*\.?[0-9]+)\s*(ms|s)\s*$")

EXPLORER_RECHECK_SECONDS = 300
"""How long a probe's answer about who answers ``/api/query`` is trusted."""

_monotonic = time.monotonic
_probe_lock = threading.Lock()
# id(connection) -> (rootserver, explorer answers, probed at). Keyed by id
# because BESConnection defines __eq__ and so is unhashable; a finalizer drops
# the entry with its connection, so a reused id can't inherit it.
_probed: dict[int, tuple[str, bool, float]] = {}


@dataclass(frozen=True)
class SessionRelevanceResult:
    """One session relevance query's answer, as the root server gave it.

    Upstream: a return type for a ``session_relevance_result`` method on
    ``BESConnection``.
    """

    relevance: str
    answers: tuple[str, ...] = ()
    """Answer text; a tuple answer joined with ``", "``, the way qna shows it."""
    answer_types: tuple[str, ...] = ()
    """One per answer, e.g. ``"integer"`` or ``"( integer, string )"``."""
    plural: bool | None = None
    """The server's ``Plurality``; None when it reported an error."""
    evaluation_time: str | None = None
    """The server's ``Evaluation/Time``, as sent (e.g. ``"0.31ms"``)."""
    error: str | None = None
    """The server's relevance error message (``Query/Error``)."""

    @property
    def ok(self) -> bool:
        return self.error is None

    @property
    def evaluation_ms(self) -> float | None:
        """``evaluation_time`` in milliseconds; None when absent or in an unknown unit."""
        match = _TIME_RE.match(self.evaluation_time or "")
        if match is None:
            return None
        value = float(match.group(1))
        return value if match.group(2) == "ms" else value * 1000


def _render(node: ET.Element) -> tuple[str, str]:
    """``(text, type)`` of an ``Answer`` or a (possibly nested) ``Tuple``."""
    if node.tag == "Tuple":
        parts = [_render(child) for child in node if child.tag in ("Answer", "Tuple")]
        text = ", ".join(t for t, _ in parts)
        return text, f"( {', '.join(k for _, k in parts)} )"
    return node.text or "", node.get("type", "")


def parse_query_result(relevance: str, xml_text: str) -> SessionRelevanceResult:
    """Parse an ``/api/query`` XML response.

    Raises ``ValueError`` when the text is not a query result at all (e.g. an
    HTML login page); a relevance error is a result, with ``error`` set.
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise ValueError(f"not a BESAPI query result: {exc}") from exc
    query = root.find("Query")
    if root.tag != "BESAPI" or query is None:
        raise ValueError(f"not a BESAPI query result: <{root.tag}>")

    error = query.find("Error")
    if error is not None:
        return SessionRelevanceResult(relevance, error=error.text or "unknown error")

    result = query.find("Result")
    rendered = [
        _render(node)
        for node in (result if result is not None else ())
        if node.tag in ("Answer", "Tuple")
    ]
    plurality = query.findtext("Evaluation/Plurality")
    return SessionRelevanceResult(
        relevance,
        answers=tuple(text for text, _ in rendered),
        answer_types=tuple(kind for _, kind in rendered),
        plural={"Plural": True, "Singular": False}.get(plurality or ""),
        evaluation_time=query.findtext("Evaluation/Time"),
    )


def session_relevance_result(conn: Any, relevance: str, **kwargs: Any) -> SessionRelevanceResult:
    """Evaluate ``relevance`` through ``conn`` (a ``BESConnection``) and parse the answer.

    ``kwargs`` go to the request (e.g. ``timeout``). Blocking. Raises
    ``ValueError`` naming the HTTP status when the server didn't answer with a
    query result, and whatever ``requests`` raises for a network failure.

    Upstream: a ``BESConnection.session_relevance_result`` method.
    """
    response = post_session_relevance(conn, relevance, **kwargs)
    try:
        return parse_query_result(relevance, response.text)
    except ValueError as exc:
        status = getattr(getattr(response, "request", None), "status_code", None)
        raise ValueError(f"HTTP {status}: {exc}") from exc


def forget_query_encoding(conn: Any = None) -> None:
    """Drop the cached probe answer for ``conn``, or for every connection.

    Upstream: reset on ``BESConnection.login()`` and on a ``rootserver`` change.
    """
    with _probe_lock:
        if conn is None:
            _probed.clear()
        else:
            _probed.pop(id(conn), None)


def explorer_answers_query(conn: Any, **kwargs: Any) -> bool:
    """Whether BigFix Explorer answers ``/api/query`` on ``conn``'s root server.

    Probes with an ``application/json+relevance`` query of ``1``: only Explorer
    answers that with 200 (Web Reports alone gives 503). Anything else,
    including an exception, is False. Cached per connection and root server for
    ``EXPLORER_RECHECK_SECONDS``. ``kwargs`` go to the probe's request.

    Upstream: a cached ``BESConnection`` method.
    """
    root = str(getattr(conn, "rootserver", ""))
    now = _monotonic()
    with _probe_lock:
        cached = _probed.get(id(conn))
    if cached is not None and cached[0] == root and now - cached[2] < EXPLORER_RECHECK_SECONDS:
        return cached[1]

    headers = {**kwargs.pop("headers", {}), "Content-Type": "application/json+relevance"}
    try:
        response = conn.post(
            "query", data=json.dumps({"relevance": "1"}), headers=headers, **kwargs
        )
        status = getattr(getattr(response, "request", None), "status_code", None)
        explorer = status == 200
        _logger.debug("query encoding probe of %s: HTTP %s", root, status)
    except Exception:  # an older server, auth, network: assume Web Reports
        _logger.debug("query encoding probe of %s failed", root, exc_info=True)
        explorer = False
    with _probe_lock:
        if id(conn) not in _probed:
            weakref.finalize(conn, _probed.pop, id(conn), None)
        _probed[id(conn)] = (root, explorer, now)
    return explorer


def post_session_relevance(conn: Any, relevance: str, **kwargs: Any) -> Any:
    """POST ``relevance`` to ``/api/query`` through ``conn``, encoded for whoever answers.

    Without ``%`` this is besapi 4.2.0's request, encoded once: decoding it a
    second time changes nothing, so it is right whether Explorer (decodes once)
    or Web Reports (decodes twice) answers, and needs no probe. With ``%`` the
    two differ (``%2541`` must stay ``%2541``), so ask ``explorer_answers_query``:
    Explorer gets it encoded once, anything else gets besapi 4.3.1+'s double
    encoding. ``kwargs`` go to the request (e.g. ``timeout``). Returns what
    ``conn.post`` returns.

    Upstream: the body builder behind ``BESConnection.session_relevance_xml``.
    """
    if "%" not in relevance or explorer_answers_query(conn, **kwargs):
        return conn.post(
            "query", data="relevance=" + urllib.parse.quote(relevance, safe=":"), **kwargs
        )
    return conn.post(
        "query", data={"relevance": urllib.parse.quote(relevance, safe=":+")}, **kwargs
    )
