"""Evaluation glue: target form -> Targets, streamed results, session relevance, breakdown.

Async, but free of NiceGUI so it can be tested without a browser. Every
upstream keyword (``transport_factory``, ``resolver``, ``compat_memory``, ...)
passes straight through ``**upstream``, which is how the tests run the real
orchestrator against fakes.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import threading
import time
from collections.abc import AsyncGenerator, AsyncIterator, Callable, Mapping, Sequence
from contextlib import aclosing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from bigfix_remote_client_relevance import (
    ClientRelevanceResult,
    InventoryError,
    Target,
    count_work,
    evaluate_client_relevance_stream,
    evaluate_many_stream,
    load_inventory,
)
from bigfix_remote_client_relevance.bootstrap.targets import host_arch, normalize_arch
from bigfix_remote_client_relevance.discovery import ImageSource, default_image_source
from bigfix_remote_client_relevance.render import display_host

from bigfix_relevance_debugger.analysis import AnalysisView, LevelView
from bigfix_relevance_debugger.upstream.besapi import session_relevance_result
from bigfix_relevance_debugger.upstream.relevance_analyzer import summarize_count_results
from bigfix_relevance_debugger.upstream.remote_client_relevance import (
    SUPPORTED_ARCHS,
    published_arches,
    target_platform,
)

logger = logging.getLogger(__name__)

DEFAULT_CONTAINER_IMAGE = "debian:11"
# One list for the arch picker and the "auto" lookup, so they can't drift.
ARCHES = SUPPORTED_ARCHS
AUTO_ARCH = "auto"
"""Every arch the image's registry manifest publishes (this machine's if unknown)."""


# --- targets ---------------------------------------------------------------------


class TargetFormError(ValueError):
    """The target picker describes nothing runnable; the message is user-facing."""


@dataclass
class TargetForm:
    """What the target picker holds. Mutable: the UI binds widgets to it."""

    local: bool = False

    container: bool = False
    container_images: str = DEFAULT_CONTAINER_IMAGE
    """Comma-separated, so one run can compare several images."""
    container_arches: list[str] = field(default_factory=lambda: [AUTO_ARCH])
    rebuild_image: bool = False

    ssh: bool = False
    ssh_host: str = ""
    """A host name or an alias from ``~/.ssh/config`` (which is also where a
    key or port goes: the library's default transport factory doesn't pass
    those through from a Target)."""
    ssh_user: str = ""
    ssh_become: bool = False
    ssh_verify_host_key: bool = True

    inventory_selected: list[int] | None = field(default_factory=list)
    """Indexes into the loaded inventory's targets. None when the picker is cleared."""


def _images(form: TargetForm) -> list[str]:
    return [i.strip() for i in form.container_images.split(",") if i.strip()]


def build_targets(
    form: TargetForm,
    inventory: Sequence[Target] = (),
    image_arches: Mapping[str, Sequence[str]] | None = None,
) -> list[Target]:
    """Turn the picker into Targets, the way the library CLI builds them.

    ``image_arches`` is what :func:`resolve_container_arches` found for the
    ``auto`` arch; an image missing from it runs as this machine's arch.
    """
    targets: list[Target] = []

    if form.local:
        # become stays None: default_transport_factory resolves it, with the
        # macOS-needs-root rule the CLI relies on too.
        targets.append(Target(kind="local", name="local"))

    if form.container:
        images = _images(form)
        if not images:
            raise TargetFormError("Container target needs an image, e.g. debian:11.")
        if not form.container_arches:
            raise TargetFormError("Container target needs at least one arch.")
        for image in images:
            arches: list[str] = []
            for choice in form.container_arches:
                if choice == AUTO_ARCH:
                    arches.extend((image_arches or {}).get(image) or [host_arch()])
                else:
                    arches.append(choice)
            arches = list(dict.fromkeys(arches))
            targets.extend(
                Target(
                    kind="container",
                    name=f"{image}@{arch}" if len(arches) > 1 else image,
                    image=image,
                    arch=arch,
                    rebuild_image=form.rebuild_image,
                )
                for arch in arches
            )

    if form.ssh:
        host = form.ssh_host.strip()
        if not host:
            raise TargetFormError("SSH target needs a host (an ~/.ssh/config alias works).")
        targets.append(
            Target(
                kind="ssh",
                name=host,
                user=form.ssh_user.strip() or None,
                become=form.ssh_become,
                verify_host_key=form.ssh_verify_host_key,
            )
        )

    selected = form.inventory_selected or []
    targets.extend(inventory[i] for i in selected if 0 <= i < len(inventory))

    if not targets:
        raise TargetFormError("Pick at least one target.")
    return targets


# --- choosing the breakdown target --------------------------------------------------

# Library platform key -> analyzer platform. The analyzer folds Raspberry Pi
# OS into debian and has no SUSE at all, so a SUSE target's fit is unknown.
_ANALYZER_PLATFORMS = {
    "macos": "macos",
    "windows": "windows",
    "debian": "debian",
    "raspbian": "debian",
    "ubuntu": "ubuntu",
    "rhel": "rhel",
}

# Tie-break among equally suitable targets: containers are the reliable path,
# the online evaluator sends the relevance to a third-party service.
_KIND_PREFERENCE = ("container", "ssh", "local", "fastquery", "online_evaluator")


def analyzer_platform(target: Target) -> str | None:
    """The target's platform in the analyzer's vocabulary; None when unknown."""
    platform = target_platform(target)
    return _ANALYZER_PLATFORMS.get(platform) if platform is not None else None


def _native_container(target: Target, arch: str) -> bool:
    # An unset container arch runs as x86_64, the library's default.
    return target.kind == "container" and normalize_arch(target.arch or "x86_64") == arch


@dataclass(frozen=True)
class BreakdownChoice:
    target: Target
    reason: str
    """User-facing: why this target, e.g. which platform it matched."""


def choose_breakdown_target(analysis: AnalysisView, targets: Sequence[Target]) -> BreakdownChoice:
    """Pick the selected target a breakdown is most likely to be meaningful on.

    Ranked by platform fit with the expression (a known match, then unknown,
    then a known mismatch), then by kind (see ``_KIND_PREFERENCE``), then by
    selection order. Selecting a single target is how a user overrides this.
    """
    if not targets:
        raise TargetFormError("Pick at least one target.")
    if len(targets) == 1:
        return BreakdownChoice(targets[0], "the only selected target")

    wanted = set(analysis.platforms)
    native_arch = host_arch()

    def rank(item: tuple[int, Target]) -> tuple[int, int, int, int]:
        index, target = item
        platform = analyzer_platform(target)
        fit = 1 if platform is None else (0 if platform in wanted else 2)
        kind = (
            _KIND_PREFERENCE.index(target.kind)
            if target.kind in _KIND_PREFERENCE
            else len(_KIND_PREFERENCE)
        )
        # A container in this machine's arch runs natively, not emulated.
        emulated = 0 if target.kind != "container" or _native_container(target, native_arch) else 1
        return fit, kind, emulated, index

    _, best = min(enumerate(targets), key=rank)
    platform = analyzer_platform(best)
    portable = wanted >= {"debian", "macos", "rhel", "ubuntu", "windows"}
    if platform is not None and platform in wanted:
        why = f"its platform ({platform}) supports this expression"
        if portable:
            why = f"a {best.kind} target; this expression runs on every platform"
    elif platform is None:
        why = (
            f"no selected target is known to be {' / '.join(sorted(wanted))}; "
            "this one's platform is unknown"
        )
    else:
        why = (
            f"no selected target matches {' / '.join(sorted(wanted)) or 'this expression'}; "
            "expect empty or error counts"
        )
    if _native_container(best, native_arch):
        why += f" (native {native_arch})"
    return BreakdownChoice(best, f"picked from {len(targets)} selected: {why}")


async def resolve_container_arches(
    form: TargetForm,
    *,
    images_factory: Callable[[], ImageSource | None] = default_image_source,
) -> dict[str, list[str]]:
    """Look up the ``auto`` arch for each container image; {} when not needed.

    Asks the container engine (and through it the registry) in a worker
    thread, since both block.
    """
    if not form.container or AUTO_ARCH not in form.container_arches:
        return {}

    def lookup() -> dict[str, list[str]]:
        images = images_factory()
        return {image: published_arches(image, images) for image in _images(form)}

    return await asyncio.to_thread(lookup)


@dataclass(frozen=True)
class InventoryLoad:
    path: Path | None
    targets: list[Target]
    error: str | None


def load_inventory_targets(path: Path | None) -> InventoryLoad:
    """Load an inventory for the picker. A bad file is reported, never raised."""
    if path is None:
        return InventoryLoad(path=None, targets=[], error=None)
    try:
        return InventoryLoad(path=path, targets=load_inventory(path), error=None)
    except InventoryError as exc:
        return InventoryLoad(path=path, targets=[], error=str(exc))
    except Exception as exc:  # noqa: BLE001 - the picker must not take the page down
        logger.debug("inventory load failed: %r", exc)
        return InventoryLoad(path=path, targets=[], error=f"{type(exc).__name__}: {exc}")


# --- options -----------------------------------------------------------------------


@dataclass(frozen=True)
class EvalOptions:
    qna_version: str = ""
    """Free text; blank means latest."""
    timeout_s: float = 30.0
    max_parallel: int = 8

    def __post_init__(self) -> None:
        if not self.timeout_s > 0:
            raise ValueError("timeout must be greater than 0 seconds")
        if self.max_parallel < 1:
            raise ValueError("max parallel must be at least 1")

    @property
    def qna_spec(self) -> str | None:
        return self.qna_version.strip() or None

    def upstream_kwargs(self) -> dict[str, Any]:
        return {
            "qna_version": self.qna_spec,
            "timeout_s": self.timeout_s,
            "max_parallel": self.max_parallel,
        }


def total_work(targets: Sequence[Target], opts: EvalOptions) -> int:
    """The progress-bar denominator: how many results a run will yield."""
    return count_work(targets, opts.qna_spec)


# --- streaming evaluation ------------------------------------------------------------


def _generator(
    stream: AsyncIterator[ClientRelevanceResult],
) -> AsyncGenerator[ClientRelevanceResult, None]:
    """Type the upstream streams as what they are, so ``aclosing`` accepts them.

    They are async generators annotated as ``AsyncIterator``; closing one is
    what cancels its remaining work, so it has to be closable.
    """
    return cast("AsyncGenerator[ClientRelevanceResult, None]", stream)


async def stream_results(
    client_relevance: str,
    targets: Sequence[Target],
    opts: EvalOptions,
    **upstream: Any,
) -> AsyncIterator[ClientRelevanceResult]:
    """Yield results in completion order.

    Cancelling the consuming task (or abandoning this iterator) closes the
    upstream stream, which cancels the remaining work and cleans up containers
    and SSH sessions.
    """
    stream = _generator(
        evaluate_client_relevance_stream(
            client_relevance, targets, **opts.upstream_kwargs(), **upstream
        )
    )
    async with aclosing(stream):
        async for result in stream:
            yield result


# --- session relevance ----------------------------------------------------------------

SESSION_CONNECT_TIMEOUT_S = 5.0
"""Connect timeout for session queries; besapi's default waits 90 s for a dead server."""
SESSION_HOST_UNKNOWN = "BigFix server"
"""The session row's host before a connection says which server it is."""

_NO_BESAPI_CONFIG = (
    "No BigFix server is configured for session relevance. Set BES_ROOT_SERVER, "
    "BES_USER_NAME and BES_PASSWORD, or add them to a [besapi] section in "
    "~/.besapi.conf (also read: /etc/besapi.conf, ~/besapi.conf, ./besapi.conf). "
    "On a root server its own credentials are used."
)


class SessionConfigError(Exception):
    """besapi found no credentials anywhere it looks."""


def _besapi_connection() -> Any:
    """besapi's plugin connection: root server credentials, then env vars, then config files.

    Looked up on each call (not imported by name) so it can be replaced.
    """
    from besapi import plugin_utilities

    return plugin_utilities.get_besapi_connection(None)


def _server_host(conn: Any) -> str:
    root = str(getattr(conn, "rootserver", "") or "")
    return root.split("://", 1)[-1].rsplit(":", 1)[0] or SESSION_HOST_UNKNOWN


class SessionConnector:
    """One besapi connection, made on first use and reused; blocking, so call from a thread.

    besapi logs in when it connects and isn't documented as thread-safe, so
    connecting and querying both happen under one lock. A failed query drops
    the connection, so the next run connects afresh.
    """

    def __init__(self, factory: Callable[[], Any] | None = None) -> None:
        self._factory = factory
        self._lock = threading.Lock()
        self._conn: Any = None
        self.host = SESSION_HOST_UNKNOWN

    def _connect(self) -> Any:
        conn = (self._factory or _besapi_connection)()
        if conn is None:
            raise SessionConfigError(_NO_BESAPI_CONFIG)
        self.host = _server_host(conn)
        return conn

    def query(self, relevance: str, timeout_s: float) -> SessionEvaluation:
        started = time.monotonic()

        def failed(kind: str, error: str) -> SessionEvaluation:
            return SessionEvaluation(
                relevance, self.host, error=error, error_kind=kind, elapsed_ms=_ms(started)
            )

        with self._lock:
            if self._conn is None:
                try:
                    self._conn = self._connect()
                except SessionConfigError as exc:
                    return failed("config", str(exc))
                except Exception as exc:  # bad credentials, no route, ...
                    logger.debug("besapi connection failed", exc_info=True)
                    return failed("connection", f"{type(exc).__name__}: {exc}")
            try:
                result = session_relevance_result(
                    self._conn, relevance, timeout=(SESSION_CONNECT_TIMEOUT_S, timeout_s)
                )
            except Exception as exc:  # network, HTTP, a non-XML answer
                logger.debug("session query failed", exc_info=True)
                self._conn = None
                return failed("connection", f"{type(exc).__name__}: {exc}")
        return SessionEvaluation(
            relevance,
            self.host,
            answers=result.answers,
            answer_types=result.answer_types,
            plural=result.plural,
            evaluation_time=result.evaluation_time,
            error=result.error,
            error_kind=None if result.ok else "relevance",
            elapsed_ms=_ms(started),
        )


def _ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


@dataclass(frozen=True)
class SessionEvaluation:
    """One session relevance query on the root server, ready to show."""

    session_relevance: str
    host: str
    answers: tuple[str, ...] = ()
    answer_types: tuple[str, ...] = ()
    plural: bool | None = None
    evaluation_time: str | None = None
    """The server's own evaluation time, e.g. ``"0.31ms"``."""
    error: str | None = None
    error_kind: str | None = None
    """``"relevance"`` (the server rejected it), ``"config"`` (no credentials
    anywhere) or ``"connection"`` (login, network or HTTP failure)."""
    elapsed_ms: int | None = None
    """Wall time of the query, including connecting when it had to."""

    @property
    def ok(self) -> bool:
        return self.error is None


_default_connector: SessionConnector | None = None


def default_connector() -> SessionConnector:
    """The process's connector, so every page and run shares one login."""
    global _default_connector
    if _default_connector is None:
        _default_connector = SessionConnector()
    return _default_connector


async def evaluate_session(
    session_relevance: str,
    opts: EvalOptions,
    *,
    connector: SessionConnector | None = None,
) -> SessionEvaluation:
    """Evaluate on the root server through besapi. Failures come back in the result.

    Cancelling stops the wait, not the request: it runs to its timeout in its
    worker thread.
    """
    connector = connector or default_connector()
    return await asyncio.to_thread(connector.query, session_relevance, opts.timeout_s)


def session_result_to_dict(ev: SessionEvaluation) -> dict[str, Any]:
    """An export entry; ``"dialect"`` tells it apart from the client results beside it."""
    return {
        "dialect": "session",
        "host": ev.host,
        "session_relevance": ev.session_relevance,
        "answers": list(ev.answers),
        "answer_types": list(ev.answer_types),
        "plural": ev.plural,
        "evaluation_time": ev.evaluation_time,
        "error": ev.error,
        "error_kind": ev.error_kind,
        "elapsed_ms": ev.elapsed_ms,
    }


@dataclass(frozen=True)
class ResultRow:
    """One evaluation result, preformatted for a table row."""

    status: str
    """``"ok"`` or the ``error_kind``."""
    ok: bool
    host: str
    display_host: str
    """``host`` with its transport where that's ambiguous (``ssh:`` / ``fastquery:``):
    an inventory can list both for one address. Rows group and show by this."""
    answers: tuple[str, ...]
    answer_types_text: str
    qna_version_text: str
    platform_text: str
    elapsed_ms: int | None
    error: str | None
    raw_qna_output: str
    dialect: str = "client"
    """``"client"`` (qna on a target) or ``"session"`` (the root server)."""
    detail_text: str = ""
    """The expanded row's run details: qna version, or the server's evaluation."""

    @classmethod
    def from_session(cls, ev: SessionEvaluation) -> ResultRow:
        details = []
        if ev.evaluation_time:
            details.append(f"evaluated in {ev.evaluation_time} on the server")
        if ev.plural is not None:
            details.append("plural" if ev.plural else "singular")
        return cls(
            status="ok" if ev.ok else (ev.error_kind or "error"),
            ok=ev.ok,
            host=ev.host,
            display_host=ev.host,
            answers=ev.answers,
            answer_types_text=", ".join(dict.fromkeys(ev.answer_types)),
            qna_version_text="",
            platform_text="session",
            elapsed_ms=ev.elapsed_ms,
            error=ev.error,
            raw_qna_output="",
            dialect="session",
            detail_text=" · ".join(details) or "session relevance",
        )

    @classmethod
    def from_result(cls, result: ClientRelevanceResult) -> ResultRow:
        qna_version = result.qna_version or ""
        if result.qna_fallback_from:
            qna_version = f"{qna_version} (fell back from {result.qna_fallback_from})"
        return cls(
            status="ok" if result.ok else (result.error_kind or "error"),
            ok=result.ok,
            host=result.host,
            display_host=display_host(result),
            answers=tuple(result.answers),
            # Usually all one type; showing each repeat would only add noise.
            answer_types_text=", ".join(dict.fromkeys(result.answer_types)),
            qna_version_text=qna_version,
            platform_text="/".join(p for p in (result.platform, result.arch) if p),
            elapsed_ms=result.elapsed_ms,
            error=result.error,
            raw_qna_output=result.raw_qna_output or "",
            detail_text=f"qna {qna_version or '?'}",
        )

    @property
    def group(self) -> tuple[str, str]:
        """What results group by: one block per dialect and displayed host."""
        return self.dialect, self.display_host

    @property
    def answer_count(self) -> int:
        return len(self.answers)

    @property
    def rank(self) -> tuple[int, int]:
        """Sort key, best first: answered (most answers first), then ok but empty, then errors."""
        if not self.ok:
            return 2, 0
        return (0, -self.answer_count) if self.answers else (1, 0)

    def matches(self, query: str) -> bool:
        """Whether an answer or the error contains ``query``, ignoring case. Blank matches all.

        An error with no message matches on its status, which is what the
        preview shows for it. Host, platform and qna version are left out on
        purpose: in a debian:11 / qna 11.x run, ``11`` would otherwise match
        every row.
        """
        needle = query.strip().casefold()
        if not needle:
            return True
        error = self.error or ("" if self.ok else self.status)
        return any(needle in text.casefold() for text in (*self.answers, error))


# --- results view ---------------------------------------------------------------------

NO_ANSWERS = "(no answers)"

PREVIEW_CHARS = 60
"""Longest answers preview in a collapsed results-table row: about one line of
the results column in the native window's default size."""


def order_results(rows: Sequence[ResultRow]) -> list[int]:
    """Indexes into ``rows`` in display order.

    Session results come first, then client results. Grouped by
    ``display_host``, since a host listing several qna versions yields one
    result per version and they belong together (while an ssh and a fastquery
    entry for one address stay apart, as do a root server's session result and
    its own client result). Hosts are ranked by their best result (see
    ``ResultRow.rank``), then by name ignoring case, then by first arrival; a
    host's own results stay in arrival order.
    """
    groups: dict[tuple[str, str], list[int]] = {}
    for index, row in enumerate(rows):
        groups.setdefault(row.group, []).append(index)

    def host_key(
        item: tuple[tuple[str, str], list[int]],
    ) -> tuple[bool, tuple[int, int], str, int]:
        (dialect, host), indexes = item
        first = dialect != "session"
        return first, min(rows[i].rank for i in indexes), host.casefold(), indexes[0]

    return [i for _, indexes in sorted(groups.items(), key=host_key) for i in indexes]


def visible_results(rows: Sequence[ResultRow], query: str) -> list[int]:
    """``order_results``, keeping only hosts with at least one matching result.

    A matching host keeps *all* its results, so a search shows each
    computer's whole answer set rather than just the matching rows.
    """
    groups = {row.group for row in rows if row.matches(query)}
    return [i for i in order_results(rows) if rows[i].group in groups]


def _shown(answer: str) -> str:
    # relevance like "" answers one empty string; that's an answer, so show it.
    return answer or '""'


def _cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def answers_preview(row: ResultRow, limit: int = PREVIEW_CHARS) -> str:
    """One line for a collapsed row: as many whole answers as fit, then ``(+N more)``.

    An error is cut to fit too; the expanded row has it whole.
    """
    if not row.ok:
        return _cut(row.error or row.status, limit)
    if not row.answers:
        return NO_ANSWERS
    answers = [_shown(a) for a in row.answers]
    shown: list[str] = []
    length = 0
    for answer in answers:
        extra = len(answer) + (2 if shown else 0)
        if length + extra > limit:
            break
        shown.append(answer)
        length += extra
    if not shown:
        # Not even the first answer fits: cut it.
        shown = [_cut(answers[0], limit)]
    text = "; ".join(shown)
    hidden = row.answer_count - len(shown)
    return f"{text} (+{hidden} more)" if hidden else text


Cells = dict[str, str | int | bool | None]
"""A results-table row. Scalars only: NiceGUI warns list cells can crash the browser."""


def results_table_row(key: int, row: ResultRow) -> Cells:
    """The cells of a collapsed row. A row never changes once it has arrived,
    so build this once per result."""
    return {
        "id": key,
        "ok": row.ok,
        "status": row.status,
        "host": row.display_host,
        "count": row.answer_count,
        "preview": answers_preview(row),
        "platform": row.platform_text,
        "qna": row.qna_version_text,
        "elapsed_ms": row.elapsed_ms,
        "dialect": row.dialect,
    }


def results_row_details(row: ResultRow) -> Cells:
    """What an expanded row adds. Kept off collapsed rows: the whole table is
    resent on each update, and raw qna output is the bulk of it."""
    return {
        "answers": "\n".join(_shown(a) for a in row.answers) or NO_ANSWERS,
        "types": row.answer_types_text,
        "detail": row.detail_text,
        "error": row.error,
        "raw": row.raw_qna_output,
    }


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def results_summary(rows: Sequence[ResultRow], shown: int, query: str) -> str:
    """The line above the results table; says so when a search hides some of them."""
    if not rows:
        return ""
    needle = query.strip()
    if needle:
        return (
            f"Showing {shown} of {_plural(len(rows), 'result')}: "
            f'hosts with an answer or error containing "{needle}"'
        )
    answered = sum(1 for r in rows if r.ok and r.answers)
    empty = sum(1 for r in rows if r.ok and not r.answers)
    errors = len(rows) - answered - empty
    return (
        f"{_plural(len(rows), 'result')}: {answered} with answers, {empty} empty, "
        f"{_plural(errors, 'error')}"
    )


# --- breakdown ------------------------------------------------------------------------

FAILURE_OUTCOMES = frozenset({"empty-or-error", "not-evaluable", "error", "no-context"})


@dataclass(frozen=True)
class ProbeView:
    """One probe's answer, reduced to what the breakdown table shows.

    ``outcome`` is one of:

    - ``"count"``: the level produced ``total`` objects (summed over contexts)
    - ``"empty-or-error"``: 0, which relevance also returns for an error in a
      plural context -- the ambiguity is the probe design's, so it is named
    - ``"not-evaluable"``: -1, e.g. a singular reference to a missing object
    - ``"no-context"``: the probe answered nothing, i.e. its context was empty
    - ``"error"``: qna or the transport failed; see ``error``/``error_kind``
    """

    relevance: str
    outcome: str
    total: int | None
    contexts: int
    """How many context objects answered (one row each)."""
    not_evaluable: int
    error: str | None = None
    error_kind: str | None = None
    qna_version: str | None = None


@dataclass(frozen=True)
class LevelResult:
    level: LevelView
    filtered: ProbeView
    unfiltered: ProbeView | None
    is_first_failure: bool

    @property
    def qna_version(self) -> str | None:
        return self.filtered.qna_version

    @property
    def count_text(self) -> str:
        def one(p: ProbeView) -> str:
            return str(p.total) if p.total is not None else p.outcome

        # "N of M" only reads as selectivity when the filtered side has a number.
        if self.unfiltered is None or self.filtered.total is None:
            return one(self.filtered)
        return f"{one(self.filtered)} of {one(self.unfiltered)}"


def _probe_view(relevance: str, result: ClientRelevanceResult | None) -> ProbeView:
    if result is None:
        # The orchestrator owes one result per expression; this only happens
        # if the run was cut short.
        return ProbeView(relevance, "error", None, 0, 0, error="no result returned")
    version = result.qna_version
    if not result.ok:
        return ProbeView(
            relevance,
            "error",
            None,
            len(result.answers),
            0,
            error=result.error,
            error_kind=result.error_kind,
            qna_version=version,
        )
    try:
        summary = summarize_count_results(result.answers)
    except ValueError as exc:
        return ProbeView(
            relevance, "error", None, len(result.answers), 0, error=str(exc), qna_version=version
        )
    return ProbeView(
        relevance,
        summary.outcome,
        summary.total,
        summary.contexts,
        summary.not_evaluable,
        qna_version=version,
    )


def _listed_versions(target: Target) -> list[str]:
    version = target.qna_version
    return [] if version is None or isinstance(version, str) else list(version)


def breakdown_qna_note(target: Target) -> str | None:
    """Which qna a breakdown pins when the host lists several; None when it doesn't."""
    listed = _listed_versions(target)
    if len(listed) < 2:
        return None
    return f"qna {listed[0]} (first of {', '.join(listed)} listed for this host)"


async def run_breakdown(
    analysis: AnalysisView,
    target: Target,
    opts: EvalOptions,
    **upstream: Any,
) -> list[LevelResult]:
    """Evaluate every level's count probe on one target, through one transport.

    Errors come back inside the ``LevelResult``s; only an unparseable analysis
    (nothing to probe) raises.
    """
    if not analysis.can_breakdown:
        raise ValueError(analysis.breakdown_disabled_reason or "cannot break down")

    # A host can list several qna versions, and its own list beats the
    # run-wide spec, so every probe would answer once per version. Pin the
    # first, or levels would silently mix builds (see breakdown_qna_note).
    listed = _listed_versions(target)
    if len(listed) > 1:
        target = dataclasses.replace(target, qna_version=listed[0])

    # Probe text can repeat (the same sub-expression twice), so evaluate each
    # distinct text once and fan the answer back out to every level using it.
    probes = list(
        dict.fromkeys(
            p
            for level in analysis.levels
            for p in (level.probe, level.unfiltered_probe)
            if p is not None
        )
    )

    by_text: dict[str, ClientRelevanceResult] = {}
    stream = _generator(
        evaluate_many_stream(probes, [target], **opts.upstream_kwargs(), **upstream)
    )
    async with aclosing(stream):
        async for result in stream:
            by_text[result.client_relevance] = result

    first_failure: int | None = None
    views: list[tuple[LevelView, ProbeView, ProbeView | None]] = []
    for level in analysis.levels:
        filtered = _probe_view(level.probe, by_text.get(level.probe))
        unfiltered = (
            _probe_view(level.unfiltered_probe, by_text.get(level.unfiltered_probe))
            if level.unfiltered_probe is not None
            else None
        )
        views.append((level, filtered, unfiltered))

    # The whole expression is level 0 and fails whenever anything does, so it
    # only answers "where" when nothing else does. The rest come innermost
    # first (post-order: an object before the level consuming it), so the first
    # failure among them is the innermost one, i.e. the root cause.
    failing = [lv.index for lv, f, _ in views if f.outcome in FAILURE_OUTCOMES]
    deeper = [i for i in failing if i != 0]
    if deeper:
        first_failure = deeper[0]
    elif failing:
        first_failure = failing[0]

    return [
        LevelResult(level, f, u, is_first_failure=level.index == first_failure)
        for level, f, u in views
    ]
