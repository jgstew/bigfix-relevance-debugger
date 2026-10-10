"""Evaluation glue: target form -> Targets, streamed results, per-level breakdown.

Async, but free of NiceGUI so it can be tested without a browser. Every
upstream keyword (``transport_factory``, ``resolver``, ``compat_memory``, ...)
passes straight through ``**upstream``, which is how the tests run the real
orchestrator against fakes.
"""

from __future__ import annotations

import asyncio
import logging
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

from bigfix_relevance_debugger.analysis import AnalysisView, LevelView
from bigfix_relevance_debugger.upstream.relevance_analyzer import summarize_count_results
from bigfix_relevance_debugger.upstream.remote_client_relevance import (
    published_arches,
    target_platform,
)

logger = logging.getLogger(__name__)

DEFAULT_CONTAINER_IMAGE = "debian:11"
ARCHES = ("x86_64", "arm64")
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


@dataclass(frozen=True)
class ResultRow:
    """One evaluation result, preformatted for a table row."""

    status: str
    """``"ok"`` or the ``error_kind``."""
    ok: bool
    host: str
    answers: tuple[str, ...]
    answers_text: str
    answer_types_text: str
    qna_version_text: str
    platform_text: str
    elapsed_ms: int | None
    error: str | None
    raw_qna_output: str

    @classmethod
    def from_result(cls, result: ClientRelevanceResult) -> ResultRow:
        if result.ok:
            answers_text = "; ".join(result.answers) if result.answers else "(no answers)"
        else:
            answers_text = "; ".join(result.answers)
        qna_version = result.qna_version or ""
        if result.qna_fallback_from:
            qna_version = f"{qna_version} (fell back from {result.qna_fallback_from})"
        return cls(
            status="ok" if result.ok else (result.error_kind or "error"),
            ok=result.ok,
            host=result.host,
            answers=tuple(result.answers),
            answers_text=answers_text,
            # Usually all one type; showing each repeat would only add noise.
            answer_types_text=", ".join(dict.fromkeys(result.answer_types)),
            qna_version_text=qna_version,
            platform_text="/".join(p for p in (result.platform, result.arch) if p),
            elapsed_ms=result.elapsed_ms,
            error=result.error,
            raw_qna_output=result.raw_qna_output or "",
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


@dataclass(frozen=True)
class LevelResult:
    level: LevelView
    filtered: ProbeView
    unfiltered: ProbeView | None
    is_first_failure: bool

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
    if not result.ok:
        return ProbeView(
            relevance,
            "error",
            None,
            len(result.answers),
            0,
            error=result.error,
            error_kind=result.error_kind,
        )
    try:
        summary = summarize_count_results(result.answers)
    except ValueError as exc:
        return ProbeView(relevance, "error", None, len(result.answers), 0, error=str(exc))
    return ProbeView(
        relevance, summary.outcome, summary.total, summary.contexts, summary.not_evaluable
    )


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
    # only answers "where" when nothing deeper does.
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
