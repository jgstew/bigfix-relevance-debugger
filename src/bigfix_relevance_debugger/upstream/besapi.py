"""Candidates for besapi.

See ``upstream/__init__.py`` for the rules.

``BESConnection.session_relevance_array`` reads only ``Result/Answer`` text:
it returns a ``"<Nothing> ..."`` sentinel for tuple answers and for an empty
result, an ``"ERROR: ..."`` string for a relevance error, and drops the answer
types, evaluation time and plurality the server sends. The JSON output rounds
the evaluation time to whole milliseconds and re-types the answers. So this
parses the ``/api/query`` XML from ``session_relevance_xml`` instead.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Any

_TIME_RE = re.compile(r"^\s*([0-9]*\.?[0-9]+)\s*(ms|s)\s*$")


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
    response = conn.session_relevance_xml(relevance, **kwargs)
    try:
        return parse_query_result(relevance, response.text)
    except ValueError as exc:
        status = getattr(getattr(response, "request", None), "status_code", None)
        raise ValueError(f"HTTP {status}: {exc}") from exc
