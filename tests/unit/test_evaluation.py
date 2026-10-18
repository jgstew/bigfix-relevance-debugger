"""Tests for evaluation.py: form -> Targets, streaming, and the breakdown glue.

Evaluation runs the real bigfix-remote-client-relevance orchestrator with an
injected fake transport and resolver, so no Docker, qna or SSH is needed.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
from bigfix_remote_client_relevance import ClientRelevanceResult, ResolvedQna, Target
from bigfix_remote_session_relevance import (
    AsyncSessionRelevanceClient,
    Config,
    ConfigError,
    ConnectionConfig,
    ResultError,
    SessionRelevanceResult,
    TrustStore,
)

from bigfix_relevance_debugger import evaluation
from bigfix_relevance_debugger.analysis import summarize
from bigfix_relevance_debugger.evaluation import (
    NO_ANSWERS,
    PREVIEW_CHARS,
    SESSION_CONNECT_TIMEOUT_S,
    SESSION_HOST_UNKNOWN,
    TALLY_LIMIT,
    AnswerTally,
    EvalOptions,
    LevelResult,
    NoTargetsError,
    ProbeView,
    ResultRow,
    SessionClients,
    SessionEvaluation,
    TallyRow,
    TargetForm,
    TargetFormError,
    analyzer_platform,
    answers_preview,
    breakdown_qna_note,
    build_targets,
    choose_breakdown_target,
    close_session_clients,
    default_session_client,
    expected_session_results,
    inventory_table_row,
    load_inventory_targets,
    needs_discovery,
    order_results,
    pick_label,
    pick_phrase,
    preview_targets,
    resolve_container_arches,
    results_row_details,
    results_summary,
    results_table_row,
    run_breakdown,
    session_evaluation,
    session_result_to_dict,
    session_row_wanted,
    stream_results,
    stream_session,
    tally_table_row,
    total_work,
    visible_results,
    waits_for_inventory,
)

WHOSE_EXAMPLE = 'names of files whose (size of it > 1000) of folder "/etc"'


# --- fakes -------------------------------------------------------------------


class FakeTransport:
    """Answers from a table keyed by the exact relevance text.

    ``answers`` maps relevance -> list of answer rows, or -> an error string
    (reported as a relevance error, the way qna reports one). Anything not in
    the table answers ``default``.
    """

    def __init__(
        self,
        target: Target,
        answers: dict[str, list[str] | str] | None = None,
        *,
        default: list[str] | None = None,
        delay: float = 0.0,
        log: list[str] | None = None,
    ) -> None:
        self.target = target
        self.answers = answers or {}
        self.default = default if default is not None else ["1"]
        self.delay = delay
        self.log = log if log is not None else []

    async def evaluate_client_relevance(
        self, client_relevance, *, qna_path=None, qna=None, timeout_s=30.0
    ):
        self.log.append(f"start {self.target.label}")
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
        except asyncio.CancelledError:
            self.log.append(f"cancelled {self.target.label}")
            raise
        answer = self.answers.get(client_relevance, self.default)
        if isinstance(answer, str):
            return ClientRelevanceResult(
                host=self.target.label,
                transport="fake",
                client_relevance=client_relevance,
                error=answer,
                error_kind="relevance",
            )
        return ClientRelevanceResult(
            host=self.target.label,
            transport="fake",
            client_relevance=client_relevance,
            answers=list(answer),
            answer_types=["integer"] * len(answer),
            elapsed_ms=5,
        )


async def fake_resolver(spec, target):
    return ResolvedQna(version=spec or "11.0.7.61", artifact_path=Path("/cache/fake.deb"))


def inject(factory):
    return {"transport_factory": factory, "resolver": fake_resolver}


SSH = Target(kind="ssh", name="host0")


# --- build_targets -------------------------------------------------------------


def test_local_target():
    [target] = build_targets(TargetForm(local=True))

    assert target.kind == "local"
    assert target.name == "local"
    # Left unspecified so the library applies its macOS-aware default.
    assert target.become is None


def test_container_target_single_arch():
    [target] = build_targets(
        TargetForm(container=True, container_images="debian:11", container_arches=["arm64"])
    )

    assert target.kind == "container"
    assert target.image == "debian:11"
    assert target.arch == "arm64"
    assert target.rebuild_image is False
    assert target.label == "container:debian:11@arm64"


def test_container_default_image_is_debian_11_on_auto_arch():
    form = TargetForm(container=True)

    assert form.container_images == "debian:11"
    assert form.container_arches == ["auto"]


def test_auto_arch_without_a_resolver_is_this_machine(monkeypatch):
    monkeypatch.setattr(evaluation, "host_arch", lambda: "arm64")

    [target] = build_targets(TargetForm(container=True))

    assert target.arch == "arm64"


def test_auto_arch_runs_every_published_arch():
    targets = build_targets(
        TargetForm(container=True), image_arches={"debian:11": ["x86_64", "arm64"]}
    )

    assert [t.label for t in targets] == [
        "container:debian:11@x86_64",
        "container:debian:11@arm64",
    ]
    assert len({t.name for t in targets}) == 2


def test_auto_arch_per_image():
    targets = build_targets(
        TargetForm(container=True, container_images="debian:11, x86-only:1"),
        image_arches={"debian:11": ["x86_64", "arm64"], "x86-only:1": ["x86_64"]},
    )

    assert [t.label for t in targets] == [
        "container:debian:11@x86_64",
        "container:debian:11@arm64",
        "container:x86-only:1@x86_64",
    ]


def test_auto_plus_explicit_arch_is_deduplicated():
    targets = build_targets(
        TargetForm(container=True, container_arches=["auto", "arm64"]),
        image_arches={"debian:11": ["x86_64", "arm64"]},
    )

    assert [t.arch for t in targets] == ["x86_64", "arm64"]


class FakeImages:
    def __init__(self, published):
        self.published = published
        self.asked: list[str] = []

    def local_tags(self):
        return []

    def platforms(self, image):
        self.asked.append(image)
        return self.published.get(image)


async def test_resolve_container_arches_asks_the_registry_per_image():
    images = FakeImages({"debian:11": {"x86_64", "arm64"}, "x86-only:1": {"x86_64"}})

    resolved = await resolve_container_arches(
        TargetForm(container=True, container_images="debian:11, x86-only:1"),
        images_factory=lambda: images,
    )

    assert resolved == {"debian:11": ["x86_64", "arm64"], "x86-only:1": ["x86_64"]}


async def test_resolve_container_arches_skips_the_registry_when_not_needed():
    def explode():
        raise AssertionError("no engine lookup expected")

    assert await resolve_container_arches(TargetForm(), images_factory=explode) == {}
    assert (
        await resolve_container_arches(
            TargetForm(container=True, container_arches=["arm64"]), images_factory=explode
        )
        == {}
    )


async def test_resolve_container_arches_asks_only_for_images_it_doesnt_know():
    images = FakeImages({"ubuntu:24.04": {"arm64"}})

    resolved = await resolve_container_arches(
        TargetForm(container=True, container_images="debian:11, ubuntu:24.04"),
        images_factory=lambda: images,
        known={"debian:11": ["x86_64", "arm64"], "other:1": ["arm64"]},
    )

    assert images.asked == ["ubuntu:24.04"]
    assert resolved == {"debian:11": ["x86_64", "arm64"], "ubuntu:24.04": ["arm64"]}


async def test_resolve_container_arches_all_known_asks_no_engine():
    def explode():
        raise AssertionError("no engine lookup expected")

    resolved = await resolve_container_arches(
        TargetForm(container=True), images_factory=explode, known={"debian:11": ["arm64"]}
    )

    assert resolved == {"debian:11": ["arm64"]}


async def test_resolve_container_arches_default_engine_is_looked_up_at_call_time(monkeypatch):
    # Checked first, so the run before the fix fails here and never asks a real engine.
    assert inspect.signature(resolve_container_arches).parameters["images_factory"].default is None
    asked = []
    monkeypatch.setattr(evaluation, "default_image_source", lambda: asked.append(1))

    await resolve_container_arches(TargetForm(container=True))

    assert asked == [1]


async def test_resolve_container_arches_without_an_engine_is_this_machine(monkeypatch):
    from bigfix_relevance_debugger.upstream import remote_client_relevance

    monkeypatch.setattr(remote_client_relevance, "host_arch", lambda: "arm64")

    resolved = await resolve_container_arches(
        TargetForm(container=True), images_factory=lambda: None
    )

    assert resolved == {"debian:11": ["arm64"]}


def test_container_rebuild_image():
    [target] = build_targets(TargetForm(container=True, rebuild_image=True))

    assert target.rebuild_image is True


def test_container_images_times_arches():
    targets = build_targets(
        TargetForm(
            container=True,
            container_images="debian:11, ubuntu:24.04",
            container_arches=["x86_64", "arm64"],
        )
    )

    assert [t.label for t in targets] == [
        "container:debian:11@x86_64",
        "container:debian:11@arm64",
        "container:ubuntu:24.04@x86_64",
        "container:ubuntu:24.04@arm64",
    ]
    # Names must differ, as the CLI does for multi-arch runs.
    assert len({t.name for t in targets}) == 4


def test_ssh_target_with_user_and_become():
    [target] = build_targets(
        TargetForm(ssh=True, ssh_host=" lab-box ", ssh_user="admin", ssh_become=True)
    )

    assert target.kind == "ssh"
    assert target.name == "lab-box"
    assert target.user == "admin"
    assert target.become is True
    assert target.verify_host_key is True


def test_ssh_blank_user_means_ssh_config_decides():
    [target] = build_targets(TargetForm(ssh=True, ssh_host="lab-box"))

    assert target.user is None
    assert target.become is False


def test_ssh_verify_host_key_can_be_turned_off():
    [target] = build_targets(TargetForm(ssh=True, ssh_host="lab", ssh_verify_host_key=False))

    assert target.verify_host_key is False


@pytest.mark.parametrize(
    ("form", "message"),
    [
        (TargetForm(ssh=True, ssh_host="  "), "host"),
        (TargetForm(container=True, container_arches=[]), "arch"),
        (TargetForm(container=True, container_images=" , "), "image"),
        # A bad field is still a bad field when another target is fine.
        (TargetForm(local=True, ssh=True, ssh_host=""), "host"),
    ],
)
def test_a_bad_target_field_is_rejected_as_a_form_error(form, message):
    with pytest.raises(TargetFormError, match=message) as raised:
        build_targets(form)

    # Not "nothing selected": a run for both dialects must not fall back on it.
    assert not isinstance(raised.value, NoTargetsError)


def test_nothing_selected_is_rejected():
    with pytest.raises(NoTargetsError, match="target"):
        build_targets(TargetForm())


@pytest.mark.parametrize("selected", [[], None])
def test_nothing_selected_runs_on_the_whole_inventory(selected):
    inventory = [Target(kind="ssh", name="a"), Target(kind="ssh", name="b")]

    targets = build_targets(TargetForm(inventory_selected=selected), inventory)

    assert targets == inventory


def test_a_ticked_target_with_no_hosts_selected_runs_only_that():
    inventory = [Target(kind="ssh", name="a")]

    targets = build_targets(TargetForm(local=True, inventory_selected=[]), inventory)

    assert [t.kind for t in targets] == ["local"]


@pytest.mark.parametrize(
    ("form", "inventory", "discoverable", "wanted"),
    [
        (TargetForm(), [], True, True),
        # Something to run already, or a host list that can't take discovery.
        (TargetForm(), [Target(kind="ssh", name="a")], True, False),
        (TargetForm(local=True), [], True, False),
        (TargetForm(), [], False, False),
    ],
)
def test_needs_discovery(form, inventory, discoverable, wanted):
    assert needs_discovery(form, inventory, discoverable=discoverable) is wanted


def test_an_empty_inventory_file_can_take_discovery(tmp_path):
    empty = tmp_path / "remote_clients.toml"
    empty.write_text("# no hosts yet\n", encoding="utf-8")

    load = load_inventory_targets(empty)

    assert load.targets == []
    assert load.error is not None and "no [hosts.*]" in load.error
    assert load.empty


def test_a_broken_inventory_file_cannot_take_discovery(tmp_path):
    broken = tmp_path / "remote_clients.toml"
    broken.write_text("= nope", encoding="utf-8")

    assert not load_inventory_targets(broken).empty
    assert load_inventory_targets(None).empty


def test_no_targets_is_a_target_form_error():
    assert issubclass(NoTargetsError, TargetFormError)


def test_inventory_selection_by_index():
    inventory = [Target(kind="ssh", name="a"), Target(kind="ssh", name="b")]

    targets = build_targets(TargetForm(inventory_selected=[1]), inventory)

    assert targets == [inventory[1]]


def test_inventory_selection_out_of_range_selects_nothing():
    inventory = [Target(kind="ssh", name="a")]

    with pytest.raises(NoTargetsError):
        build_targets(TargetForm(inventory_selected=[5]), inventory)


def test_several_kinds_together():
    targets = build_targets(
        TargetForm(local=True, container=True, ssh=True, ssh_host="h"),
    )

    assert [t.kind for t in targets] == ["local", "container", "ssh"]


# --- inventory -----------------------------------------------------------------


def test_load_inventory_targets(tmp_path):
    path = tmp_path / "remote_clients.toml"
    path.write_text(
        '[hosts.deb]\ntransport = "container"\nimage = "debian:12"\n'
        '[hosts.lab]\ntransport = "ssh"\nuser = "me"\n',
        encoding="utf-8",
    )

    loaded = load_inventory_targets(path)

    assert loaded.error is None
    assert loaded.path == path
    assert [t.kind for t in loaded.targets] == ["container", "ssh"]


def test_load_inventory_bad_file_reports_error_not_raise(tmp_path):
    path = tmp_path / "remote_clients.toml"
    path.write_text("this is = = not toml", encoding="utf-8")

    loaded = load_inventory_targets(path)

    assert loaded.targets == []
    assert loaded.error


def test_load_inventory_empty_reports_error(tmp_path):
    path = tmp_path / "remote_clients.toml"
    path.write_text("[defaults]\n", encoding="utf-8")

    loaded = load_inventory_targets(path)

    assert loaded.targets == []
    assert loaded.error


def test_load_inventory_missing_path_is_not_an_error(tmp_path):
    loaded = load_inventory_targets(None)

    assert loaded.targets == []
    assert loaded.error is None
    assert loaded.path is None


# --- options ---------------------------------------------------------------------


def test_options_blank_qna_version_means_latest():
    assert EvalOptions(qna_version="  ").qna_spec is None
    assert EvalOptions(qna_version=" 11.0.7.61 ").qna_spec == "11.0.7.61"


def test_options_defaults():
    opts = EvalOptions()

    assert opts.timeout_s == 30.0
    assert opts.max_parallel == 8


@pytest.mark.parametrize("kwargs", [{"timeout_s": 0}, {"max_parallel": 0}])
def test_options_reject_nonsense(kwargs):
    with pytest.raises(ValueError):
        EvalOptions(**kwargs)


def test_total_work_counts_targets():
    targets = build_targets(
        TargetForm(container=True, container_arches=["x86_64", "arm64"], local=True)
    )

    assert total_work(targets, EvalOptions()) == 3


# --- stream_results ---------------------------------------------------------------


async def test_stream_results_yields_one_per_target():
    targets = [Target(kind="ssh", name=f"h{i}") for i in range(3)]

    results = [
        r
        async for r in stream_results(
            "name of operating system",
            targets,
            EvalOptions(),
            **inject(lambda t: FakeTransport(t, default=["Linux"])),
        )
    ]

    assert sorted(r.host for r in results) == ["h0", "h1", "h2"]
    assert all(r.answers == ["Linux"] for r in results)


async def test_stream_results_completion_order():
    delays = {"slow": 0.3, "fast": 0.0}
    targets = [Target(kind="ssh", name="slow"), Target(kind="ssh", name="fast")]

    hosts = [
        r.host
        async for r in stream_results(
            "true",
            targets,
            EvalOptions(),
            **inject(lambda t: FakeTransport(t, delay=delays[t.name])),
        )
    ]

    assert hosts == ["fast", "slow"]


async def test_stream_results_errors_come_back_as_rows():
    def explode(target):
        raise RuntimeError("host key verification failed")

    [result] = [r async for r in stream_results("true", [SSH], EvalOptions(), **inject(explode))]

    assert result.error_kind == "transport"
    assert "host key" in (result.error or "")


async def test_cancelling_the_consumer_cancels_remaining_work():
    log: list[str] = []
    targets = [Target(kind="ssh", name=n) for n in ("fast", "slow1", "slow2")]
    delays = {"fast": 0.0, "slow1": 5.0, "slow2": 5.0}
    first = asyncio.Event()

    async def consume():
        async for _ in stream_results(
            "true",
            targets,
            EvalOptions(),
            **inject(lambda t: FakeTransport(t, delay=delays[t.name], log=log)),
        ):
            first.set()

    task = asyncio.create_task(consume())
    await asyncio.wait_for(first.wait(), 2)
    started = time.monotonic()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert time.monotonic() - started < 1.0
    assert "cancelled slow1" in log
    assert "cancelled slow2" in log


# --- ResultRow --------------------------------------------------------------------


def test_result_row_ok():
    row = ResultRow.from_result(
        ClientRelevanceResult(
            host="container:debian:11@arm64",
            transport="container",
            client_relevance="name of operating system",
            answers=["Linux Debian 11", "x"],
            answer_types=["string", "string"],
            qna_version="11.0.7.61",
            platform="debian",
            arch="arm64",
            elapsed_ms=1234,
            raw_qna_output="Q: name of operating system\nA: Linux Debian 11\n",
        )
    )

    assert row.status == "ok"
    assert row.ok is True
    assert row.host == "container:debian:11@arm64"
    assert row.answer_types_text == "string"
    assert row.qna_version_text == "11.0.7.61"
    assert row.platform_text == "debian/arm64"
    assert row.elapsed_ms == 1234
    assert row.error is None
    assert row.raw_qna_output.startswith("Q:")


def test_result_row_notes_qna_fallback():
    row = ResultRow.from_result(
        ClientRelevanceResult(
            host="h",
            transport="ssh",
            client_relevance="true",
            answers=["True"],
            qna_version="11.0.6.137",
            qna_fallback_from="11.0.7.61",
        )
    )

    assert row.qna_version_text == "11.0.6.137 (fell back from 11.0.7.61)"


def test_result_row_error():
    row = ResultRow.from_result(
        ClientRelevanceResult(
            host="h",
            transport="ssh",
            client_relevance="true",
            error="Permission denied (publickey)",
            error_kind="transport",
        )
    )

    assert row.status == "transport"
    assert row.ok is False
    assert row.error == "Permission denied (publickey)"


def test_result_row_no_answers_is_ok_but_says_so():
    row = ResultRow.from_result(
        ClientRelevanceResult(host="h", transport="ssh", client_relevance="nothing")
    )

    assert row.ok is True
    assert results_row_details(row)["answers"] == "(no answers)"


# --- run_breakdown ----------------------------------------------------------------


def probes_of(view):
    """(filtered, unfiltered) probe text for each level."""
    return [(lv.probe, lv.unfiltered_probe) for lv in view.levels]


async def test_breakdown_maps_each_probe_to_its_level():
    view = summarize(WHOSE_EXAMPLE)
    answers: dict[str, list[str] | str] = {lv.probe: [str(10 + lv.index)] for lv in view.levels}

    levels = await run_breakdown(
        view, SSH, EvalOptions(), **inject(lambda t: FakeTransport(t, answers))
    )

    assert all(isinstance(lr, LevelResult) for lr in levels)
    assert [lr.level.index for lr in levels] == [lv.index for lv in view.levels]
    assert [lr.filtered.total for lr in levels] == [10 + lv.index for lv in view.levels]


async def test_breakdown_pairs_filtered_and_unfiltered():
    view = summarize(WHOSE_EXAMPLE)
    whose = next(lv for lv in view.levels if lv.unfiltered_probe)
    assert whose.unfiltered_probe is not None
    answers: dict[str, list[str] | str] = {whose.probe: ["21"], whose.unfiltered_probe: ["25"]}

    levels = await run_breakdown(
        view, SSH, EvalOptions(), **inject(lambda t: FakeTransport(t, answers))
    )

    result = levels[whose.index]
    assert result.filtered.total == 21
    assert result.unfiltered is not None
    assert result.unfiltered.total == 25
    assert result.count_text == "21 of 25"
    assert all(lr.unfiltered is None for lr in levels if lr.level.index != whose.index)


async def test_breakdown_uses_one_transport_for_all_probes():
    view = summarize(WHOSE_EXAMPLE)
    built: list[Target] = []

    def factory(target):
        built.append(target)
        return FakeTransport(target)

    await run_breakdown(view, SSH, EvalOptions(), **inject(factory))

    assert len(built) == 1


@pytest.mark.parametrize(
    ("rows", "outcome", "total"),
    [
        (["0"], "empty-or-error", 0),
        (["-1"], "not-evaluable", None),
        (["25"], "count", 25),
        (["3", "0", "4"], "count", 7),
        (["-1", "-1"], "not-evaluable", None),
        ([], "no-context", None),
    ],
)
async def test_breakdown_outcome_interpretation(rows, outcome, total):
    view = summarize("name of operating system")
    target_level = view.levels[1]

    levels = await run_breakdown(
        view,
        SSH,
        EvalOptions(),
        **inject(lambda t: FakeTransport(t, {target_level.probe: rows})),
    )

    probe = levels[1].filtered
    assert probe.outcome == outcome
    assert probe.total == total
    assert probe.contexts == len(rows)


async def test_breakdown_errored_probe_is_reported_not_raised():
    view = summarize(WHOSE_EXAMPLE)
    bad = view.levels[1]
    answers: dict[str, list[str] | str] = {
        bad.probe: "Singular expression refers to nonexistent object."
    }

    levels = await run_breakdown(
        view, SSH, EvalOptions(), **inject(lambda t: FakeTransport(t, answers))
    )

    assert levels[1].filtered.outcome == "error"
    assert levels[1].filtered.error == "Singular expression refers to nonexistent object."
    assert levels[1].filtered.error_kind == "relevance"
    assert levels[1].filtered.total is None


async def test_breakdown_transport_failure_is_reported_on_every_level():
    view = summarize(WHOSE_EXAMPLE)

    def explode(target):
        raise RuntimeError("cannot connect")

    levels = await run_breakdown(view, SSH, EvalOptions(), **inject(explode))

    assert levels
    assert all(lr.filtered.outcome == "error" for lr in levels)
    assert all(lr.filtered.error_kind == "transport" for lr in levels)


async def test_breakdown_non_integer_rows_are_an_error_not_a_crash():
    view = summarize("name of operating system")

    levels = await run_breakdown(
        view,
        SSH,
        EvalOptions(),
        **inject(lambda t: FakeTransport(t, {view.levels[1].probe: ["banana"]})),
    )

    assert levels[1].filtered.outcome == "error"
    assert "banana" in (levels[1].filtered.error or "")


async def test_breakdown_flags_first_failing_level_skipping_whole_expression():
    view = summarize(WHOSE_EXAMPLE)
    # Everything after the "files whose" level is empty too, but the first
    # failure in analyzer order is the answer to "why is it false?".
    answers: dict[str, list[str] | str] = {lv.probe: ["0"] for lv in view.levels}
    answers[view.levels[0].probe] = ["0"]

    levels = await run_breakdown(
        view, SSH, EvalOptions(), **inject(lambda t: FakeTransport(t, answers))
    )

    flagged = [lr.level.index for lr in levels if lr.is_first_failure]
    assert flagged == [1]


async def test_breakdown_flags_nothing_when_every_level_counts():
    view = summarize(WHOSE_EXAMPLE)

    levels = await run_breakdown(
        view, SSH, EvalOptions(), **inject(lambda t: FakeTransport(t, default=["3"]))
    )

    assert not any(lr.is_first_failure for lr in levels)


async def test_breakdown_flags_whole_expression_when_it_is_the_only_failure():
    view = summarize(WHOSE_EXAMPLE)
    answers: dict[str, list[str] | str] = {view.levels[0].probe: ["-1"]}

    levels = await run_breakdown(
        view, SSH, EvalOptions(), **inject(lambda t: FakeTransport(t, answers))
    )

    assert [lr.level.index for lr in levels if lr.is_first_failure] == [0]


async def test_breakdown_duplicate_probe_text_feeds_every_level():
    # Two levels with identical probe text must both get the answer, not
    # just the first one to claim it.
    view = summarize("(name of operating system) & (name of operating system)")
    dupes = [lv for lv in view.levels if [x.probe for x in view.levels].count(lv.probe) > 1]
    assert dupes, "precondition: this expression yields duplicate probes"

    levels = await run_breakdown(
        view, SSH, EvalOptions(), **inject(lambda t: FakeTransport(t, default=["2"]))
    )

    assert all(lr.filtered.total == 2 for lr in levels)


async def test_breakdown_refuses_unparseable_text():
    view = summarize("name of (operating system")

    with pytest.raises(ValueError, match="parse"):
        await run_breakdown(view, SSH, EvalOptions(), **inject(lambda t: FakeTransport(t)))


def _probe(outcome, total=None, error=None):
    return ProbeView("p", outcome, total, 1, 0, error=error)


def _level():
    return summarize(WHOSE_EXAMPLE).levels[1]


def test_count_text_errored_whose_level_does_not_repeat_itself():
    lr = LevelResult(_level(), _probe("error", error="x"), _probe("error", error="x"), False)

    assert lr.count_text == "error"


def test_count_text_filtered_empty_of_unfiltered_count():
    lr = LevelResult(_level(), _probe("empty-or-error", 0), _probe("count", 25), False)

    assert lr.count_text == "0 of 25"


def test_count_text_without_unfiltered():
    lr = LevelResult(_level(), _probe("not-evaluable"), None, False)

    assert lr.count_text == "not-evaluable"


def test_cleared_inventory_selection_means_no_inventory_targets():
    # A cleared multi-select reports None, not [].
    inventory = [Target(kind="ssh", name="a")]

    targets = build_targets(TargetForm(local=True, inventory_selected=None), inventory)

    assert [t.kind for t in targets] == ["local"]


# --- choosing the breakdown target ----------------------------------------------------

REGISTRY = 'exists key "HKLM\\\\Software" of registry'
RPM = 'names of rpm package "bash" of rpm'


def container(image):
    return Target(kind="container", name=image, image=image, arch="x86_64")


@pytest.mark.parametrize(
    ("target", "platform"),
    [
        (Target(kind="ssh", name="pi", platform="raspbian"), "debian"),
        (Target(kind="ssh", name="sles", platform="suse"), None),
        (container("opensuse/leap:15"), None),
        (container("almalinux:9"), "rhel"),
        (Target(kind="ssh", name="unknown"), None),
    ],
)
def test_analyzer_platform_translates_library_keys(target, platform):
    # The analyzer has no raspbian (it is debian) and no SUSE at all (unknown fit).
    assert analyzer_platform(target) == platform


def test_single_target_is_used_as_is():
    only = Target(kind="ssh", name="only")

    choice = choose_breakdown_target(summarize(REGISTRY), [only])

    assert choice.target is only
    assert "only selected target" in choice.reason


def test_platform_match_wins():
    windows = Target(kind="ssh", name="win", platform="windows")

    choice = choose_breakdown_target(summarize(REGISTRY), [container("debian:11"), windows])

    assert choice.target is windows
    assert "windows" in choice.reason


def test_rpm_expression_picks_rpm_family_container():
    alma = container("almalinux:9")

    choice = choose_breakdown_target(summarize(RPM), [container("debian:11"), alma])

    assert choice.target is alma


def test_known_match_beats_unknown_platform():
    windows = Target(kind="ssh", name="win", platform="windows")

    choice = choose_breakdown_target(
        summarize(REGISTRY), [Target(kind="ssh", name="unknown"), windows]
    )

    assert choice.target is windows


def test_unknown_platform_beats_known_mismatch():
    unknown = Target(kind="ssh", name="unknown")

    choice = choose_breakdown_target(summarize(REGISTRY), [container("debian:11"), unknown])

    assert choice.target is unknown


def test_portable_expression_prefers_containers_then_ssh_and_avoids_online_evaluator():
    deb = container("debian:11")
    targets = [
        Target(kind="online_evaluator", name="dev", base_url="https://example.invalid"),
        Target(kind="local", name="local", platform="macos"),
        Target(kind="ssh", name="lab", platform="ubuntu"),
        deb,
    ]

    assert choose_breakdown_target(summarize("name of operating system"), targets).target is deb


def test_ties_keep_selection_order():
    first, second = container("debian:11"), container("debian:12")

    choice = choose_breakdown_target(summarize("name of operating system"), [first, second])

    assert choice.target is first


def test_no_compatible_target_still_picks_one_and_says_so():
    deb = container("debian:11")

    choice = choose_breakdown_target(summarize(REGISTRY), [deb])

    assert choice.target is deb
    choice = choose_breakdown_target(summarize(REGISTRY), [deb, container("ubuntu:24.04")])
    assert "windows" in choice.reason
    assert "no selected target" in choice.reason


def test_choose_from_nothing_is_an_error():
    with pytest.raises(NoTargetsError):
        choose_breakdown_target(summarize(REGISTRY), [])


@pytest.mark.parametrize(
    ("machine", "native"), [("arm64", "arm64"), ("aarch64", "arm64"), ("AMD64", "x86_64")]
)
def test_native_arch_container_preferred(monkeypatch, machine, native):
    monkeypatch.setattr("platform.machine", lambda: machine)
    x86 = Target(kind="container", name="d", image="debian:12")  # unset arch means x86_64
    arm = Target(kind="container", name="d@arm64", image="debian:11", arch="arm64")
    expected = arm if native == "arm64" else x86

    choice = choose_breakdown_target(summarize("name of operating system"), [x86, arm])

    assert choice.target is expected
    assert native in choice.reason


def test_platform_fit_still_beats_native_arch(monkeypatch):
    monkeypatch.setattr("platform.machine", lambda: "arm64")
    alma_x86 = Target(kind="container", name="alma", image="almalinux:9", arch="x86_64")
    deb_arm = Target(kind="container", name="deb", image="debian:11", arch="arm64")

    choice = choose_breakdown_target(summarize(RPM), [deb_arm, alma_x86])

    assert choice.target is alma_x86


# --- review fixes ----------------------------------------------------------------


class VersionedTransport:
    """Answers "1" under qna 11.0.6.1 and "0" under anything else."""

    def __init__(self, target):
        self.target = target

    async def evaluate_client_relevance(
        self, client_relevance, *, qna_path=None, qna=None, timeout_s=30.0
    ):
        version = qna.version if qna else None
        return ClientRelevanceResult(
            host=self.target.label,
            transport="fake",
            client_relevance=client_relevance,
            answers=["1" if version == "11.0.6.1" else "0"],
            qna_version=version,
        )


async def test_breakdown_pins_one_qna_version_when_a_host_lists_several():
    view = summarize(WHOSE_EXAMPLE)
    target = Target(kind="ssh", name="multi", qna_version=["11.0.6.1", "11.0.7.2"])

    levels = await run_breakdown(
        view, target, EvalOptions(), transport_factory=VersionedTransport, resolver=fake_resolver
    )

    # Every level from the first listed version, never a mix.
    assert {lr.filtered.total for lr in levels} == {1}
    assert {lr.qna_version for lr in levels} == {"11.0.6.1"}


def test_breakdown_version_pin_is_named(monkeypatch):
    assert breakdown_qna_note(Target(kind="ssh", name="m", qna_version=["a", "b"])) == (
        "qna a (first of a, b listed for this host)"
    )
    assert breakdown_qna_note(Target(kind="ssh", name="m", qna_version="a")) is None
    assert breakdown_qna_note(Target(kind="ssh", name="m")) is None


def test_arch_picker_and_arch_lookup_share_one_list():
    from bigfix_relevance_debugger.upstream.remote_client_relevance import SUPPORTED_ARCHS

    assert evaluation.ARCHES is SUPPORTED_ARCHS


# --- results view (issue 4) ---------------------------------------------------------


def row(host, answers=(), *, error=None, error_kind=None, qna_version=None, transport="ssh"):
    if error is not None and error_kind is None:
        error_kind = "transport"
    return ResultRow.from_result(
        ClientRelevanceResult(
            host=host,
            transport=transport,
            client_relevance="x",
            answers=list(answers),
            error=error,
            error_kind=error_kind,
            qna_version=qna_version,
        )
    )


def hosts(rows, order):
    return [rows[i].host for i in order]


def test_order_puts_most_answers_first_then_empty_then_errors():
    rows = [row("empty"), row("err", error="boom"), row("three", "abc"), row("one", "a")]

    assert hosts(rows, order_results(rows)) == ["three", "one", "empty", "err"]


def test_order_breaks_ties_by_host_name_ignoring_case_then_arrival():
    rows = [row("b", "x"), row("A", "y"), row("c", "z")]

    assert hosts(rows, order_results(rows)) == ["A", "b", "c"]


def test_order_keeps_a_hosts_results_together_ranked_by_its_best():
    # One host listing two qna versions yields two results with the same host.
    rows = [
        row("multi", qna_version="a"),
        row("x", "1"),
        row("multi", "12", qna_version="b"),
    ]

    order = order_results(rows)

    assert hosts(rows, order) == ["multi", "multi", "x"]
    assert [rows[i].qna_version_text for i in order[:2]] == ["a", "b"]  # arrival order


def test_order_of_a_prefix_never_reorders_rows_within_a_host():
    rows = [row("h", qna_version="a"), row("g", "1"), row("h", "12", qna_version="b")]

    for n in range(1, len(rows) + 1):
        within_h = [i for i in order_results(rows[:n]) if rows[i].host == "h"]
        assert within_h == sorted(within_h)


def test_match_is_a_case_insensitive_substring_of_an_answer():
    r = row("h", ["Linux Debian 11", "other"])

    assert r.matches("debian")
    assert r.matches("  DEBIAN ")
    assert not r.matches("ubuntu")


def test_match_includes_the_error_message():
    assert row("h", error="Permission denied (publickey)").matches("permission")


def test_blank_query_matches_everything():
    assert row("h").matches("")
    assert row("h", error="boom").matches("   ")


def test_match_ignores_host_platform_and_qna_version():
    # Otherwise "11" would match every row of a debian:11 / qna 11.x run.
    r = row("container:debian:11", ["Linux"], qna_version="11.0.7.61")

    assert not r.matches("11")


def test_filter_hides_hosts_without_a_matching_result():
    rows = [row("deb", ["Linux Debian 11"]), row("rh", ["Linux Red Hat 9"])]

    assert hosts(rows, visible_results(rows, "debian")) == ["deb"]


def test_filter_keeps_every_result_of_a_matching_host():
    rows = [
        row("multi", ["Linux Debian 12"], qna_version="a"),
        row("multi", qna_version="b"),
        row("rh", ["Linux Red Hat 9"]),
    ]

    assert visible_results(rows, "debian") == [0, 1]


def test_filter_returns_display_order():
    rows = [row("one", ["debian"]), row("three", ["debian", "b", "c"]), row("no", ["x"])]

    assert visible_results(rows, "debian") == [i for i in order_results(rows) if i != 2]
    assert visible_results(rows, "") == order_results(rows)


def test_table_row_cells_are_scalars():
    # NiceGUI: list cells can crash the browser.
    r = ResultRow.from_result(
        ClientRelevanceResult(
            host="h",
            transport="container",
            client_relevance="x",
            answers=["a", "b"],
            answer_types=["string", "string"],
            qna_version="11.0.7.61",
            platform="debian",
            arch="arm64",
            elapsed_ms=12,
            raw_qna_output="Q: x\nA: a\nA: b\n",
        )
    )

    cells = results_table_row(7, r)
    details = results_row_details(r)

    assert cells["id"] == 7
    for values in (cells.values(), details.values()):
        assert all(isinstance(v, str | int | bool | type(None)) for v in values)
    assert cells["count"] == 2
    assert cells["host"] == "h"
    assert cells["platform"] == "debian/arm64"
    assert cells["qna"] == "11.0.7.61"
    assert cells["dialect"] == "client"
    assert details == {
        "answers": "a\nb",
        "types": "string",
        "detail": "qna 11.0.7.61",
        "error": None,
        "raw": "Q: x\nA: a\nA: b\n",
    }


def test_collapsed_row_cells_leave_out_the_details():
    # Every streamed result resends the whole table; only open rows carry the bulk.
    cells = results_table_row(0, row("h", ["a" * 1000]))

    assert not set(cells) & {"answers", "types", "error", "raw"}


def test_details_show_an_empty_string_answer_as_quotes():
    # relevance like "" answers one empty string: that is an answer, not none.
    assert results_row_details(row("h", [""]))["answers"] == '""'
    assert results_row_details(row("h", ["a", ""]))["answers"] == 'a\n""'
    assert results_row_details(row("h"))["answers"] == "(no answers)"


def test_preview_shows_an_empty_string_answer_as_quotes():
    assert answers_preview(row("h", [""])) == '""'


def test_ssh_and_fastquery_entries_for_one_address_are_separate_hosts():
    # An inventory can list both for one address; render.display_host tells them apart.
    rows = [
        row("192.168.4.115", ["via ssh"], transport="ssh"),
        row("192.168.4.115", ["via fastquery"], transport="fastquery"),
    ]

    assert [r.display_host for r in rows] == ["ssh:192.168.4.115", "fastquery:192.168.4.115"]
    assert visible_results(rows, "via ssh") == [0]
    assert results_table_row(1, rows[1])["host"] == "fastquery:192.168.4.115"


def test_container_display_host_is_the_host():
    assert row("container:debian:11@arm64", transport="container").display_host == (
        "container:debian:11@arm64"
    )


def test_match_includes_the_status_of_an_error_without_a_message():
    # The preview shows the status then, so the filter has to find it too.
    timed_out = row("h", error_kind="timeout")

    assert answers_preview(timed_out) == "timeout"
    assert timed_out.matches("timeout")
    assert not row("h", ["a"]).matches("ok")  # an ok row's status isn't shown


def test_preview_fits_whole_answers_then_says_how_many_more():
    answers = [f"answer-{i:03d}" for i in range(30)]  # 10 chars each

    preview = answers_preview(row("h", answers), limit=120)

    assert preview == "; ".join(answers[:10]) + " (+20 more)"


def test_default_preview_fits_a_line_of_the_results_column():
    # Checked in a real run: 120 characters wrapped to three lines at 1400 px.
    assert PREVIEW_CHARS <= 60


def test_preview_cuts_a_single_long_answer_with_an_ellipsis():
    preview = answers_preview(row("h", ["x" * 500, "y"]), limit=20)

    assert preview == "x" * 19 + "… (+1 more)"


def test_preview_shows_everything_when_it_fits():
    assert answers_preview(row("h", ["a", "b"])) == "a; b"


def test_preview_cuts_a_long_error_too():
    preview = answers_preview(row("h", error="e" * 500), limit=20)

    assert preview == "e" * 19 + "…"


def test_preview_for_empty_and_error_rows_never_reads_none():
    assert answers_preview(row("h")) == "(no answers)"
    assert answers_preview(row("h", error="boom")) == "boom"
    no_message = ResultRow.from_result(
        ClientRelevanceResult(host="h", transport="ssh", client_relevance="x", error_kind="qna")
    )
    assert answers_preview(no_message) == "qna"


def test_summary_counts_answered_empty_and_errors():
    rows = [row("a", ["1"]), row("b"), row("c", error="boom")]

    assert results_summary(rows, 3, "") == "3 results: 1 with answers, 1 empty, 1 error"


def test_summary_pluralises():
    rows = [row("a", error="x"), row("b", error="y")]

    assert results_summary(rows, 2, "") == "2 results: 0 with answers, 0 empty, 2 errors"
    assert results_summary(rows[:1], 1, "") == "1 result: 0 with answers, 0 empty, 1 error"


def test_summary_when_filtered_says_showing_n_of_m_and_the_query():
    rows = [row("a", ["debian"]), row("b"), row("c", error="boom")]

    assert results_summary(rows, 1, " debian ") == (
        'Showing 1 of 3 results: hosts with an answer or error containing "debian"'
    )


def test_summary_of_nothing_is_empty():
    assert results_summary([], 0, "") == ""


# --- session relevance (issues 7 and 25) ------------------------------------------------

SESSION_FIXTURES = Path(__file__).parent / "fixtures" / "session_rest"
SERVER_URL = "https://bigfix.example.com:52311"


def rest_body(name):
    return json.loads((SESSION_FIXTURES / f"{name}.json").read_text())


def rest_conn(name, server=SERVER_URL, *, deployment_id: str | None = "", **fields):
    """A REST connection; its deployment id defaults to one of its own (no discovery)."""
    fields.setdefault("username", "api")
    fields.setdefault("password", "pw")
    return ConnectionConfig(
        name=name,
        backend="rest",
        server=server,
        deployment_id=(f"dep-{name}" if deployment_id == "" else deployment_id),
        **fields,
    )


class Router:
    """Fake root servers behind ``httpx.MockTransport``, one answer per connection.

    ``answers[name]`` is a response body (a dict, sent as JSON), an
    ``httpx.Response``, an exception to raise, or an async callable taking the
    relevance. Discovery's deployment-id query gets the captured answer.
    """

    def __init__(self, **answers: Any):
        self.answers: dict[str, Any] = answers
        self.queries: list[tuple[str, str]] = []

    def transport(self, conn):
        async def handle(request):
            relevance = json.loads(request.content)["relevance"]
            self.queries.append((conn.name, relevance))
            if relevance == "sha256 of gather url of bes license":
                return httpx.Response(200, json=rest_body("deployment_id"))
            answer: Any = self.answers.get(conn.name, rest_body("plural_tuples"))
            if inspect.iscoroutinefunction(answer):
                answer = await answer(relevance)
            if isinstance(answer, BaseException):
                raise answer
            if isinstance(answer, httpx.Response):
                return answer
            return httpx.Response(200, json=answer)

        return httpx.MockTransport(handle)


def session_clients(*conns, router=None, loads=None):
    """``SessionClients`` over the real default factory, with a fake config and server.

    ``loads`` replaces ``load_config``: configs (or exceptions to raise) to
    hand out in turn, the last one repeating. By default it returns ``conns``.
    """
    router = router or Router()
    built: list[AsyncSessionRelevanceClient] = []
    pending: list[Config | Exception] = list(loads or [Config(connections=conns)])

    def load() -> Config:
        item = pending.pop(0) if len(pending) > 1 else pending[0]
        if isinstance(item, Exception):
            raise item
        return item

    def factory(opts):
        client = default_session_client(
            opts, load=load, transport_factory=router.transport, trust=None
        )
        built.append(client)
        return client

    clients = SessionClients(factory)
    clients.built = built  # type: ignore[attr-defined]
    return clients


async def collect_session(text, opts=None, *, clients):
    return [ev async for ev in stream_session(text, opts or EvalOptions(), clients=clients)]


async def test_session_streams_one_result_per_deployment_as_each_answers():
    async def slow(relevance):
        await asyncio.sleep(0.05)
        return rest_body("plural_tuples")

    clients = session_clients(
        rest_conn("prod"),
        rest_conn("lab", "https://lab.example.com:52311"),
        router=Router(prod=slow),
    )

    evs = await collect_session('(1, "a")', clients=clients)

    assert [ev.connection for ev in evs] == ["lab", "prod"]  # completion order
    assert [ev.host for ev in evs] == ["lab.example.com", "bigfix.example.com"]
    assert {ev.backend for ev in evs} == {"rest"}
    assert [ev.deployment_id for ev in evs] == ["dep-lab", "dep-prod"]
    assert all(ev.session_relevance == '(1, "a")' for ev in evs)


async def test_connections_to_one_deployment_give_one_result():
    clients = session_clients(
        rest_conn("root", deployment_id="same"),
        rest_conn("root-again", deployment_id="same"),
    )

    evs = await collect_session("now", clients=clients)

    assert len(evs) == 1


async def test_session_answers_render_the_way_qna_does():
    [ev] = await collect_session("x", clients=session_clients(rest_conn("prod")))

    assert ev.ok
    assert ev.answers == ("1, a", "2, b")
    assert ev.answer_types == ("( integer, string )",)
    assert ev.plural is True
    # Forwarded to BigFix Explorer, which reports fractions of a millisecond.
    assert ev.evaluation_time == "0.049ms"
    assert ev.elapsed_ms is not None and ev.elapsed_ms >= 0


async def test_session_empty_plural_answer():
    clients = session_clients(rest_conn("prod"), router=Router(prod=rest_body("empty_plural")))

    [ev] = await collect_session("x", clients=clients)

    assert ev.ok
    assert ev.answers == ()
    assert ev.plural is True
    assert ev.evaluation_time == "0ms"


async def test_session_relevance_error_is_a_result_not_a_raise():
    clients = session_clients(rest_conn("prod"), router=Router(prod=rest_body("error_unknown")))

    [ev] = await collect_session("foo bar baz", clients=clients)

    assert not ev.ok
    assert ev.error_kind == "session_relevance"
    assert ev.error == 'The operator "foo bar baz" is not defined.'
    assert ev.connection == "prod"


async def test_session_without_any_config_is_one_config_result():
    [ev] = await collect_session("now", clients=session_clients())

    assert not ev.ok
    assert ev.error_kind == "config"
    assert ev.host == SESSION_HOST_UNKNOWN
    assert ev.error is not None
    for where in ("--setup", "BES_ROOT_SERVER", "BES_USER_NAME", "BES_PASSWORD", "~/.besapi.conf"):
        assert where in ev.error
    # besapi's other files are no longer read, so they aren't suggested.
    assert "/etc/besapi.conf" not in ev.error


async def test_disabled_connections_count_as_none_configured():
    [ev] = await collect_session("now", clients=session_clients(rest_conn("off", enabled=False)))

    assert ev.error_kind == "config"


async def test_session_with_a_broken_config_file_says_why(tmp_path, monkeypatch):
    broken = tmp_path / "connections.toml"
    broken.write_text("[[connections]\n", encoding="utf-8")
    monkeypatch.setenv("BFRSR_CONFIG", str(broken))
    clients = SessionClients(lambda opts: default_session_client(opts, trust=None))

    [ev] = await collect_session("now", clients=clients)

    assert ev.error_kind == "config"
    assert ev.error is not None and str(broken) in ev.error


@pytest.mark.parametrize(
    ("answer", "kind"),
    [
        (httpx.ConnectError("connection refused"), "connection"),
        (httpx.Response(401, text="HTTP 401: Unauthorized"), "authentication"),
    ],
)
async def test_session_failures_are_results(answer, kind):
    clients = session_clients(rest_conn("prod"), router=Router(prod=answer))

    [ev] = await collect_session("now", clients=clients)

    assert ev.error_kind == kind
    assert ev.connection == "prod"
    assert ev.host == "bigfix.example.com"


async def test_session_without_a_password_never_prompts():
    router = Router()
    clients = session_clients(rest_conn("prod", password=None), router=router)

    [ev] = await collect_session("now", clients=clients)

    assert ev.error_kind == "authentication"
    assert ev.error is not None and "no password" in ev.error
    assert router.queries == []


async def test_a_config_mistake_found_while_querying_is_a_config_result():
    # The BES_* fallback isn't validated when loaded; a bad server surfaces
    # only when the library builds its backends. The next run rebuilds.
    bad = rest_conn("bad", "https://bigfix.example.com:52311/some/path")
    clients = session_clients(
        loads=[Config(connections=(bad,)), Config(connections=(rest_conn("prod"),))]
    )

    first = await collect_session("now", clients=clients)
    second = await collect_session("now", clients=clients)

    [ev] = first
    assert ev.error_kind == "config"
    assert ev.error is not None and "path" in ev.error
    assert [ev.ok for ev in second] == [True]
    assert len(clients.built) == 2
    assert clients.built[0]._backends == {}  # closed


async def test_an_unreadable_config_is_a_config_result():
    clients = session_clients(loads=[PermissionError("~/.bigfix: permission denied")])

    [ev] = await collect_session("now", clients=clients)

    assert ev.error_kind == "config"
    assert ev.error is not None and "permission denied" in ev.error
    assert expected_session_results(session_clients(loads=[OSError("nope")])) == 1


async def test_session_client_is_built_once_and_reused():
    clients = session_clients(rest_conn("prod"))

    await collect_session("now", clients=clients)
    await collect_session("now", clients=clients)

    assert len(clients.built) == 1


async def test_each_timeout_gets_its_own_client_and_keeps_it():
    # Two pages with different timeouts take turns: neither evicts the other's
    # client, which would sign in again (SaaS) and forget discovered ids.
    clients = session_clients(rest_conn("prod"))

    for timeout_s in (30, 12, 30, 12):
        await collect_session("now", EvalOptions(timeout_s=timeout_s), clients=clients)

    thirty, twelve = clients.built
    assert [c.read_timeout for c in thirty.connections] == [30.0]
    assert [c.read_timeout for c in twelve.connections] == [12.0]
    assert thirty._backends and twelve._backends  # neither closed


async def test_the_least_recently_used_client_is_closed_past_the_limit(monkeypatch):
    monkeypatch.setattr(evaluation, "SESSION_CLIENTS_KEPT", 2)
    clients = session_clients(rest_conn("prod"))

    for timeout_s in (10, 20, 10, 30):
        await collect_session("now", EvalOptions(timeout_s=timeout_s), clients=clients)
    await asyncio.sleep(0)

    ten, twenty, thirty = clients.built
    assert twenty._backends == {}  # least recently used: closed
    assert ten._backends and thirty._backends


async def test_session_client_is_rebuilt_after_a_config_failure():
    # The user ran --setup after the first run; no restart needed.
    clients = session_clients(
        loads=[ConfigError("cannot parse"), Config(connections=(rest_conn("prod"),))]
    )

    first = await collect_session("now", clients=clients)
    second = await collect_session("now", clients=clients)

    assert [ev.error_kind for ev in first] == ["config"]
    assert [ev.ok for ev in second] == [True]


async def test_session_client_is_rebuilt_after_nothing_was_configured():
    clients = session_clients(loads=[Config(), Config(connections=(rest_conn("prod"),))])

    first = await collect_session("now", clients=clients)
    second = await collect_session("now", clients=clients)

    assert [ev.error_kind for ev in first] == ["config"]
    assert [ev.ok for ev in second] == [True]


def test_session_timeouts_reach_every_connection():
    config = Config(connections=(rest_conn("a"), rest_conn("b", connect_timeout=60)))

    client = default_session_client(EvalOptions(timeout_s=12), load=lambda: config, trust=None)

    assert [(c.connect_timeout, c.read_timeout) for c in client.connections] == [
        (SESSION_CONNECT_TIMEOUT_S, 12.0),
        (SESSION_CONNECT_TIMEOUT_S, 12.0),
    ]


def test_default_session_client_never_prompts_and_trusts_on_first_use():
    config = Config(connections=(rest_conn("a"),))

    client = default_session_client(EvalOptions(), load=lambda: config)

    assert client._prompt is None
    assert isinstance(client._trust, TrustStore)
    assert client._trust.callback is None
    assert client._trust.trust_on_first_use


async def test_leaving_the_stream_early_cancels_the_other_queries():
    cancelled = asyncio.Event()

    async def hang(relevance):
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    clients = session_clients(rest_conn("fast"), rest_conn("hung"), router=Router(hung=hang))

    stream = stream_session("now", EvalOptions(), clients=clients)
    first = await anext(stream)
    await stream.aclose()

    assert first.connection == "fast"
    await asyncio.wait_for(cancelled.wait(), 2)


async def test_an_evicted_client_waits_for_streams_still_using_it(monkeypatch):
    # Evicted while a page's run is still reading from it: closed only after.
    monkeypatch.setattr(evaluation, "SESSION_CLIENTS_KEPT", 1)
    release = asyncio.Event()

    async def held(relevance):
        if relevance == "now":  # only the first run's query
            await release.wait()
        return rest_body("plural_tuples")

    clients = session_clients(rest_conn("prod"), router=Router(prod=held))
    first_run = asyncio.create_task(
        collect_session("now", EvalOptions(timeout_s=30), clients=clients)
    )
    while not clients.built:
        await asyncio.sleep(0)
    await asyncio.sleep(0.01)

    await collect_session("other", EvalOptions(timeout_s=12), clients=clients)
    await asyncio.sleep(0)
    assert clients.built[0]._backends  # still open: the first run uses it

    release.set()
    evs = await first_run

    assert all(ev.ok for ev in evs)
    assert clients.built[0]._backends == {}  # closed once its stream ended


class RaisingClient:
    """A client whose stream yields ``before`` results, then raises ``exc``."""

    def __init__(self, exc, before=0):
        self.exc = exc
        self.before = before
        self.connections = (rest_conn("prod"), rest_conn("lab"))
        self.closed = False

    def result_count(self):
        return 2

    async def evaluate_stream(self, session_relevance):
        for _ in range(self.before):
            yield session_result(connection="prod")
        raise self.exc

    async def aclose(self):
        self.closed = True


@pytest.mark.parametrize("before", [0, 1])
async def test_an_unexpected_stream_failure_is_a_result(before, caplog):
    client = RaisingClient(
        OSError("cannot write ~/.bigfix/.remote_session_known_certs.toml"), before
    )
    clients = SessionClients(lambda opts: client)  # type: ignore[arg-type, return-value]

    with caplog.at_level(logging.DEBUG, logger=evaluation.__name__):
        evs = await collect_session("now", clients=clients)

    *answered, failed = evs
    assert [ev.connection for ev in answered] == ["prod"] * before
    assert failed.error_kind == "error"
    assert failed.error is not None
    assert "OSError" in failed.error and "known_certs" in failed.error
    assert failed.host == SESSION_HOST_UNKNOWN
    assert "session relevance stream failed" in caplog.text
    assert caplog.records[-1].exc_info is not None
    # Kept: the failure may be passing (a file write), and rebuilding would
    # sign in again and forget discovered deployment ids.
    await collect_session("now", clients=clients)
    assert not client.closed


async def test_cancelling_a_session_stream_is_not_turned_into_a_result():
    started = asyncio.Event()

    class Hanging(RaisingClient):
        async def evaluate_stream(self, session_relevance):
            started.set()
            await asyncio.sleep(30)
            yield session_result()  # pragma: no cover

    clients = SessionClients(lambda opts: Hanging(None))  # type: ignore[arg-type, return-value]
    task = asyncio.create_task(collect_session("now", clients=clients))
    await started.wait()

    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task


async def test_session_clients_aclose_closes_the_client():
    clients = session_clients(rest_conn("prod"))
    await collect_session("now", clients=clients)

    await clients.aclose()

    assert clients.built[0]._backends == {}


async def test_close_session_clients_closes_the_process_clients(monkeypatch):
    clients = session_clients(rest_conn("prod"))
    await collect_session("now", clients=clients)
    monkeypatch.setattr(evaluation, "_session_clients", clients)

    await close_session_clients()

    assert clients.built[0]._backends == {}
    assert evaluation._session_clients is None


async def test_close_session_clients_without_any_is_a_no_op():
    await close_session_clients()

    assert evaluation._session_clients is None


async def test_expected_session_results_counts_deployments():
    clients = session_clients(
        rest_conn("a", deployment_id="one"),
        rest_conn("b", deployment_id="one"),
        rest_conn("c", deployment_id="two"),
    )

    assert expected_session_results(clients) == 2


async def test_expected_session_results_before_discovery_counts_connections():
    clients = session_clients(
        rest_conn("a", deployment_id=None),
        rest_conn("b", "https://other.example.com:52311", deployment_id=None),
    )

    assert expected_session_results(clients) == 2
    evs = await collect_session("now", clients=clients)

    # Both report the captured deployment id: one deployment after all.
    assert len(evs) == 1
    assert expected_session_results(clients) == 1


def test_expected_session_results_with_nothing_configured_is_the_config_row():
    assert expected_session_results(session_clients()) == 1


def test_expected_session_results_with_a_broken_config_is_the_config_row():
    assert expected_session_results(session_clients(loads=[ConfigError("bad")])) == 1


def test_the_default_session_clients_use_default_session_client(monkeypatch):
    # conftest makes the real factory fail loudly; this checks it is the one used.
    built = []

    def fake(opts):
        built.append(opts)
        return default_session_client(
            opts, load=lambda: Config(connections=(rest_conn("prod"),)), trust=None
        )

    monkeypatch.setattr(evaluation, "default_session_client", fake)

    assert expected_session_results() == 1
    assert built == [EvalOptions()]


def session_result(**fields):
    fields.setdefault("connection", "prod")
    fields.setdefault("backend", "rest")
    fields.setdefault("server", SERVER_URL)
    fields.setdefault("deployment_id", "dep-prod")
    fields.setdefault("elapsed_ms", 12.4)
    error = fields.pop("error", None)
    if error is not None:
        return SessionRelevanceResult.failure("x", error, **fields)
    fields.setdefault("type", "( integer, string )")
    fields.setdefault("plural", True)
    fields.setdefault("evaluation_time_ms", 0.47)
    values = fields.pop("values", [[1, "a"], [2, "b"]])
    return SessionRelevanceResult.from_values("x", values, **fields)


def test_session_evaluation_maps_every_field():
    ev = session_evaluation(session_result())

    assert ev == SessionEvaluation(
        "x",
        "bigfix.example.com",
        answers=("1, a", "2, b"),
        answer_types=("( integer, string )",),
        plural=True,
        evaluation_time="0.47ms",
        elapsed_ms=12,
        connection="prod",
        backend="rest",
        deployment_id="dep-prod",
    )


@pytest.mark.parametrize(
    ("server", "host"),
    [
        ("https://bigfix.example.com:52311", "bigfix.example.com"),
        ("https://bigfix.example.com", "bigfix.example.com"),
        ("bigfix.example.com:52311", "bigfix.example.com"),
        ("https://[2001:db8::1]:52311", "2001:db8::1"),
        (None, SESSION_HOST_UNKNOWN),
        ("", SESSION_HOST_UNKNOWN),
    ],
)
def test_session_evaluation_host_is_the_server_host(server, host):
    assert session_evaluation(session_result(server=server)).host == host


@pytest.mark.parametrize(
    ("ms", "text"),
    [
        (0, "0ms"),
        (0.049, "0.049ms"),
        (0.31, "0.31ms"),
        (4.0, "4ms"),
        (1234567.8, "1234567.8ms"),
    ],
)
def test_session_evaluation_time_reads_plainly(ms, text):
    assert session_evaluation(session_result(evaluation_time_ms=ms)).evaluation_time == text


def test_session_evaluation_of_an_error():
    error = ResultError(kind="tls", message="certificate changed", backend="rest")

    ev = session_evaluation(session_result(error=error))

    assert not ev.ok
    assert ev.error == "certificate changed"
    assert ev.error_kind == "tls"
    assert ev.answers == ()
    assert ev.answer_types == ()
    assert ev.evaluation_time is None


def session_row(**fields):
    return ResultRow.from_session(session_evaluation(session_result(**fields)))


def test_session_result_row():
    r = session_row()

    assert r.dialect == "session"
    assert r.ok
    assert r.status == "ok"
    assert r.host == "bigfix.example.com"
    assert r.display_host == "prod"  # the connection: which deployment answered
    assert r.answers == ("1, a", "2, b")
    assert r.answer_types_text == "( integer, string )"
    assert r.platform_text == "session"
    assert r.qna_version_text == ""
    assert "via rest" in r.detail_text
    assert "0.47ms" in r.detail_text
    assert "plural" in r.detail_text


def test_session_row_without_a_connection_shows_the_host():
    r = ResultRow.from_session(
        SessionEvaluation("x", SESSION_HOST_UNKNOWN, error="e", error_kind="config")
    )

    assert r.display_host == SESSION_HOST_UNKNOWN


def test_session_rows_group_by_connection():
    rows = [
        session_row(connection="prod"),
        session_row(connection="lab"),
        session_row(connection="prod"),
    ]

    assert [r.group for r in rows] == [("session", "prod"), ("session", "lab"), ("session", "prod")]


def test_client_result_row_is_client_dialect():
    assert row("h", ["a"]).dialect == "client"


def test_session_error_row():
    r = session_row(
        error=ResultError(kind="session_relevance", message='The operator "nme" is not defined.')
    )

    assert not r.ok
    assert r.status == "session_relevance"
    assert r.error == 'The operator "nme" is not defined.'


@pytest.mark.parametrize(
    ("error_kind", "optional", "wanted"),
    [
        (None, True, True),
        # Nobody asked for session and no server is configured: not an error to show.
        ("config", True, False),
        # A server is configured but unreachable: worth knowing either way.
        ("connection", True, True),
        ("session_relevance", True, True),
        # Session was asked for, so say why it didn't run.
        ("config", False, True),
    ],
)
def test_session_row_wanted(error_kind, optional, wanted):
    ev = evaluation.SessionEvaluation(
        "now", SESSION_HOST_UNKNOWN, error="x" if error_kind else None, error_kind=error_kind
    )

    assert session_row_wanted(ev, optional=optional) is wanted


def test_session_rows_list_first_even_when_they_fail():
    failed = session_row(error=ResultError(kind="session_relevance", message="nope"))
    rows = [row("three", "abc"), failed, row("one", "a")]

    assert order_results(rows)[0] == 1


def test_session_and_client_rows_for_one_host_stay_apart():
    # A root server can also be an inventory host (fastquery or ssh to it).
    rows = [row("bigfix.example.com", "a", transport="local"), session_row(connection=None)]

    order = order_results(rows)

    assert order == [1, 0]
    assert results_table_row(1, rows[1])["dialect"] == "session"
    assert results_table_row(0, rows[0])["dialect"] == "client"


def test_session_result_dict_for_export():
    d = session_result_to_dict(session_evaluation(session_result()))

    assert d == {
        "dialect": "session",
        "host": "bigfix.example.com",
        "connection": "prod",
        "backend": "rest",
        "deployment_id": "dep-prod",
        "session_relevance": "x",
        "answers": ["1, a", "2, b"],
        "answer_types": ["( integer, string )"],
        "plural": True,
        "evaluation_time": "0.47ms",
        "error": None,
        "error_kind": None,
        "elapsed_ms": 12,
    }


# --- answer summary (issue 8) ----------------------------------------------------------


def tallied(*rows):
    tally = AnswerTally()
    for r in rows:
        tally.add(r)
    return tally


def counts(tally, limit=TALLY_LIMIT):
    shown, _ = tally.rows(limit)
    return [(r.label, r.systems) for r in shown]


def server_row(answers=("x",)):
    return ResultRow.from_session(
        evaluation.SessionEvaluation("q", "bigfix.example.com", answers=tuple(answers))
    )


def test_tally_counts_distinct_systems_per_answer():
    tally = tallied(row("a", ["Linux"]), row("b", ["Linux"]), row("c", ["Win"]))

    assert counts(tally) == [("Linux", 2), ("Win", 1)]
    assert tally.systems == 3


def test_tally_counts_a_host_once_across_its_qna_versions():
    tally = tallied(row("multi", ["x"], qna_version="a"), row("multi", ["x"], qna_version="b"))

    assert counts(tally) == [("x", 1)]
    assert tally.systems == 1


def test_tally_counts_a_repeated_answer_once_per_host():
    assert counts(tallied(row("h", ["x", "x"]))) == [("x", 1)]


def test_tally_host_whose_versions_disagree_counts_under_each():
    tally = tallied(row("multi", ["x"], qna_version="a"), row("multi", [], qna_version="b"))

    assert counts(tally) == [("x", 1), (NO_ANSWERS, 1)]


def test_tally_counts_systems_by_display_host():
    # An inventory can list one address as both ssh and fastquery: two systems.
    tally = tallied(row("10.0.0.5", ["x"]), row("10.0.0.5", ["x"], transport="fastquery"))

    assert counts(tally) == [("x", 2)]
    assert tally.systems == 2


def test_tally_orders_by_systems_then_answer_then_arrival():
    tally = tallied(
        row("1", ["B"]), row("2", ["a"]), row("3", ["top"]), row("4", ["top"]), row("5", ["c"])
    )

    assert counts(tally) == [("top", 2), ("a", 1), ("B", 1), ("c", 1)]


def test_tally_puts_no_answers_then_errors_last():
    tally = tallied(
        row("err1", error="boom", error_kind="transport"),
        *(row(f"empty{i}") for i in range(5)),
        row("one", ["x"]),
    )

    assert counts(tally) == [("x", 1), (NO_ANSWERS, 5), ("error: transport", 1)]


def test_tally_groups_errors_by_kind():
    tally = tallied(
        row("a", error="Permission denied", error_kind="transport"),
        row("b", error="No route to host", error_kind="transport"),
        row("c", error="qna crashed", error_kind="qna"),
    )

    assert counts(tally) == [("error: transport", 2), ("error: qna", 1)]


def test_tally_limit_hides_the_rarest_answers_and_counts_them():
    common = [f"common{i:02}" for i in range(50)]
    rare = [f"rare{i}" for i in range(10)]
    tally = tallied(row("a", common + rare), row("b", common), row("c"))

    shown, hidden = tally.rows(TALLY_LIMIT)

    assert hidden == 10
    assert [r.label for r in shown] == [*common, NO_ANSWERS]


def test_tally_shows_an_empty_string_answer_as_quotes():
    assert counts(tallied(row("a", [""]))) == [('""', 1)]


def test_tally_ignores_session_rows():
    tally = tallied(server_row(), row("a", ["x"]))

    assert counts(tally) == [("x", 1)]
    assert tally.systems == 1


def test_tally_table_row_cells_are_scalars():
    tally = tallied(row("a", ["x"]), row("b", error="boom"))
    shown, _ = tally.rows()
    assert len(shown) == 2

    for r in shown:
        cells = tally_table_row(r, tally.systems, pick=None)
        assert all(isinstance(v, str | int | float | bool | type(None)) for v in cells.values())


def test_tally_table_row_share_and_percent():
    tally = tallied(row("a", ["x"]), row("b", ["y"]), row("c", ["y"]))
    [y, x] = tally.rows()[0]

    cells = tally_table_row(x, tally.systems, pick=None)

    assert cells["share"] == pytest.approx(1 / 3)
    assert cells["percent"] == 33
    assert cells["systems"] == 1
    assert cells["kind"] == "answer"
    assert tally_table_row(y, tally.systems, pick=None)["percent"] == 67


def test_tally_table_row_marks_the_pick():
    tally = tallied(row("a", ["x"]), row("b", ["y"]))
    [x, y] = tally.rows()[0]

    assert tally_table_row(x, 2, pick=x.key)["picked"] is True
    assert tally_table_row(y, 2, pick=x.key)["picked"] is False


def test_has_key_answer_is_exact_and_case_sensitive():
    debian = row("a", ["Linux Debian 11"])

    assert debian.has_key(TallyRow("answer", "Linux Debian 11", 1).key)
    assert not debian.has_key(TallyRow("answer", "linux debian 11", 1).key)
    assert not debian.has_key(TallyRow("answer", "Linux", 1).key)


def test_has_key_for_no_answers_and_error_kind():
    empty = TallyRow("empty", "", 1).key
    transport = TallyRow("error", "transport", 1).key

    assert row("a").has_key(empty)
    assert not row("a", ["x"]).has_key(empty)
    assert not row("a", error="boom").has_key(empty)
    assert row("a", error="boom", error_kind="transport").has_key(transport)
    assert not row("a", error="boom", error_kind="qna").has_key(transport)


def test_pick_key_round_trips_answers_containing_colons():
    key = TallyRow("answer", "a:b:c", 1).key

    assert row("h", ["a:b:c"]).has_key(key)
    assert pick_phrase(key) == 'answering "a:b:c"'


def test_pick_labels_match_the_summary_rows():
    for r in (TallyRow("answer", "a:b", 1), TallyRow("empty", "", 1), TallyRow("error", "qna", 1)):
        assert pick_label(r.key) == r.label


def test_pick_phrases():
    assert pick_phrase(TallyRow("answer", "", 1).key) == 'answering """"'
    assert pick_phrase(TallyRow("empty", "", 1).key) == "with no answers"
    assert pick_phrase(TallyRow("error", "transport", 1).key) == "with an error (transport)"
    assert pick_phrase(TallyRow("error", "auth", 1).key) == "with an error (auth)"


def test_an_error_without_a_kind_reads_as_plain_error():
    # ResultRow.status falls back to "error" with no error_kind. The library's
    # failed results always have one, but the wording mustn't read "error error".
    unknown = TallyRow("error", "error", 1)

    assert unknown.label == "error"
    assert pick_phrase(unknown.key) == "with an error"


PICK_FLEET = [
    row("deb", ["Linux Debian 11"], qna_version="a"),
    row("rh", ["Linux Red Hat 9"], qna_version="a"),
    row("multi", ["Linux Debian 11"], qna_version="a"),
    row("multi", [], qna_version="b"),
    row("down", error="Permission denied", error_kind="transport"),
    row("plural", ["Linux Debian 11", "extra"]),
]


def test_visible_results_with_pick_keeps_every_result_of_a_matching_host():
    key = TallyRow("empty", "", 1).key

    assert hosts(PICK_FLEET, visible_results(PICK_FLEET, "", key)) == ["multi", "multi"]


def test_visible_results_pick_and_query_combine():
    key = TallyRow("answer", "Linux Debian 11", 1).key

    assert set(hosts(PICK_FLEET, visible_results(PICK_FLEET, "", key))) == {
        "deb",
        "multi",
        "plural",
    }
    assert hosts(PICK_FLEET, visible_results(PICK_FLEET, "extra", key)) == ["plural"]


def test_visible_results_without_pick_is_unchanged():
    for query in ("", "debian", "denied", "nothing"):
        assert visible_results(PICK_FLEET, query, None) == visible_results(PICK_FLEET, query)


def test_pick_shows_exactly_the_tallied_systems():
    rows = [*PICK_FLEET, row("other", error="crash", error_kind="qna"), row("e2")]
    tally = tallied(*rows)
    tallies, _ = tally.rows()
    assert len(tallies) == 6  # 3 answers, no answers, 2 error kinds

    for r in tallies:
        shown = {rows[i].display_host for i in visible_results(rows, "", r.key)}
        assert len(shown) == r.systems, r.label


def test_session_rows_never_match_a_pick():
    rows = [server_row(["x"]), row("a", ["x"])]

    assert hosts(rows, visible_results(rows, "", TallyRow("answer", "x", 1).key)) == ["a"]


def test_summary_with_pick_says_which_answer():
    debian = TallyRow("answer", "Linux Debian 11", 1).key

    assert (
        results_summary(PICK_FLEET, 4, "", debian)
        == 'Showing 4 of 6 results: hosts answering "Linux Debian 11"'
    )
    assert results_summary(PICK_FLEET, 2, "", TallyRow("empty", "", 1).key).endswith(
        "hosts with no answers"
    )
    assert results_summary(PICK_FLEET, 1, "", TallyRow("error", "transport", 1).key).endswith(
        "hosts with an error (transport)"
    )


def test_summary_with_pick_and_query():
    debian = TallyRow("answer", "Linux Debian 11", 1).key

    assert results_summary(PICK_FLEET, 1, " extra ", debian) == (
        'Showing 1 of 6 results: hosts answering "Linux Debian 11" '
        'and an answer or error containing "extra"'
    )


def test_summary_query_only_is_unchanged():
    assert results_summary(PICK_FLEET, 3, "debian") == (
        'Showing 3 of 6 results: hosts with an answer or error containing "debian"'
    )


# --- target preview (issue 8) ------------------------------------------------------------


def test_inventory_table_row():
    deb = Target(kind="container", name="deb", image="debian:11", qna_version=["1.0", "2.0"])

    assert inventory_table_row(0, deb) == {
        "id": 0,
        "label": "container:debian:11@x86_64",
        "kind": "container",
        "platform": "debian",
        "about": "container · debian",
        "qna": "1.0, 2.0",
    }
    assert inventory_table_row(1, Target(kind="ssh", name="box"))["platform"] == "?"
    assert inventory_table_row(1, Target(kind="ssh", name="box"))["qna"] == ""
    assert inventory_table_row(2, Target(kind="ssh", name="b", qna_version="1.0"))["qna"] == "1.0"


INV = [Target(kind="ssh", name=f"lab-{c}") for c in "abc"]


def preview(form, inventory=INV, image_arches=None, **kwargs):
    kwargs = {"qna_spec": None, "inventory_pending": False} | kwargs
    return preview_targets(form, inventory, image_arches or {}, **kwargs)


def test_preview_counts_inventory_selection_and_explicit_targets():
    p = preview(TargetForm(local=True, inventory_selected=[0, 2]))

    assert p.systems == 3
    assert p.exact
    assert p.problem is None
    assert p.text == "3 systems"
    assert "2 inventory" in p.detail
    assert "1 local" in p.detail


def test_preview_with_nothing_selected_counts_the_whole_inventory():
    p = preview(TargetForm())

    assert p.problem is None
    assert p.systems == len(INV)
    assert p.sources == (("inventory", len(INV)),)


def test_preview_with_nothing_to_run_and_discovery_left_says_so():
    p = preview(TargetForm(), inventory=[], discoverable=True)

    assert p.problem is None
    assert p.discovers
    assert p.text == "auto-discovery"
    assert "auto-discovery" in p.detail
    assert (
        preview(TargetForm(), inventory=[], discoverable=True, session=True).text
        == "auto-discovery + BigFix server"
    )


def test_preview_with_nothing_selected_reports_the_form_problem():
    p = preview(TargetForm(), inventory=[])

    assert p.systems == 0
    assert p.problem == "Pick at least one target."
    assert p.text == "0 systems"
    assert p.problem in p.detail


def test_preview_container_without_image_is_the_build_targets_message():
    p = preview(TargetForm(container=True, container_images=" "))

    with pytest.raises(TargetFormError) as raised:
        build_targets(TargetForm(container=True, container_images=" "))
    assert p.problem == str(raised.value)


def test_preview_auto_arch_unknown_is_a_lower_bound(monkeypatch):
    monkeypatch.setattr(evaluation, "host_arch", lambda: "arm64")

    p = preview(TargetForm(container=True, inventory_selected=[]))

    assert p.systems == 1
    assert not p.exact
    assert p.text == "≥ 1 system"
    assert p.unresolved_images == ("debian:11",)
    assert "debian:11" in p.detail


def test_preview_auto_arch_known_is_exact():
    p = preview(TargetForm(container=True), image_arches={"debian:11": ["x86_64", "arm64"]})

    assert p.systems == 2
    assert p.exact
    assert p.unresolved_images == ()
    assert p.text == "2 systems"
    assert "2 container" in p.detail


def test_preview_explicit_arches_need_no_lookup():
    p = preview(TargetForm(container=True, container_arches=["arm64"]))

    assert p.text == "1 system"
    assert p.exact
    assert p.unresolved_images == ()


def test_preview_waits_for_inventory_like_evaluate():
    pending = preview(TargetForm(), inventory=[], inventory_pending=True)

    assert pending.systems is None
    assert pending.problem is None
    assert pending.text == "finding inventory …"

    explicit = preview(TargetForm(local=True), inventory=[], inventory_pending=True)
    assert explicit.systems == 1
    assert explicit.exact


def test_waits_for_inventory():
    assert waits_for_inventory(TargetForm(inventory_selected=[0]), pending=False)
    assert waits_for_inventory(TargetForm(), pending=True)
    assert not waits_for_inventory(TargetForm(local=True), pending=True)
    assert not waits_for_inventory(TargetForm(), pending=False)


def test_preview_counts_runs_when_hosts_list_several_qna_versions():
    inv = [Target(kind="ssh", name="multi", qna_version=["a", "b"]), Target(kind="ssh", name="x")]

    p = preview(TargetForm(inventory_selected=[0, 1]), inventory=inv)

    assert p.systems == 2
    assert p.runs == 3
    assert "3 runs" in p.detail


def test_preview_says_nothing_about_runs_when_they_equal_systems():
    detail = preview(TargetForm(inventory_selected=[0, 1])).detail

    assert detail == "2 inventory"


@pytest.mark.parametrize(
    ("form", "arches"),
    [
        (TargetForm(local=True), {}),
        (TargetForm(ssh=True, ssh_host="box"), {}),
        (TargetForm(container=True, container_arches=["x86_64", "arm64"]), {}),
        (TargetForm(container=True, container_images="a:1, b:2"), {"a:1": ["x86_64", "arm64"]}),
        (TargetForm(inventory_selected=[1]), {}),
        (TargetForm(local=True, inventory_selected=None), {}),
        (TargetForm(), {}),
        (TargetForm(inventory_selected=None), {}),
        (TargetForm(local=True, ssh=True, ssh_host="b", inventory_selected=[0, 1, 2, 7]), {}),
    ],
)
def test_preview_matches_build_targets(form, arches):
    # The count must never drift from what Evaluate runs.
    assert preview(form, image_arches=arches).systems == len(build_targets(form, INV, arches))


def test_preview_text_singular_and_plural():
    assert preview(TargetForm(local=True, inventory_selected=[])).text == "1 system"
    assert preview(TargetForm(inventory_selected=[0, 1])).text == "2 systems"


def test_preview_session_only_needs_no_targets():
    p = preview(TargetForm(), client=False, session=True)

    assert p.problem is None
    assert p.text == "BigFix server"
    assert "session" in p.detail


def test_preview_both_adds_the_server():
    assert (
        preview(TargetForm(inventory_selected=[0, 1]), session=True).text
        == "2 systems + BigFix server"
    )


def test_preview_both_without_client_targets_runs_session_only():
    p = preview(TargetForm(), inventory=[], session=True)

    assert p.problem is None
    assert p.text == "BigFix server"


def test_preview_counts_several_bigfix_servers():
    p = preview(TargetForm(), client=False, session=True, session_servers=2)

    assert p.text == "2 BigFix servers"
    assert "session relevance on 2 BigFix servers" in p.detail
    assert (
        preview(TargetForm(inventory_selected=[0, 1]), session=True, session_servers=3).text
        == "2 systems + 3 BigFix servers"
    )


def test_preview_both_with_a_bad_target_is_still_a_problem():
    p = preview(TargetForm(ssh=True), session=True)

    assert p.problem is not None
    assert "SSH" in p.problem
