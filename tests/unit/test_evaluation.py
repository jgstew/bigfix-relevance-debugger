"""Tests for evaluation.py: form -> Targets, streaming, and the breakdown glue.

Evaluation runs the real bigfix-remote-client-relevance orchestrator with an
injected fake transport and resolver, so no Docker, qna or SSH is needed.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest
from bigfix_remote_client_relevance import ClientRelevanceResult, ResolvedQna, Target

from bigfix_relevance_debugger.analysis import summarize
from bigfix_relevance_debugger.evaluation import (
    EvalOptions,
    LevelResult,
    ProbeView,
    ResultRow,
    TargetForm,
    TargetFormError,
    build_targets,
    choose_breakdown_target,
    load_inventory_targets,
    run_breakdown,
    stream_results,
    target_platform,
    total_work,
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


def test_container_default_image_is_debian_11():
    [target] = build_targets(TargetForm(container=True))

    assert target.image == "debian:11"
    assert target.arch == "x86_64"


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


def test_ssh_without_host_is_rejected():
    with pytest.raises(TargetFormError, match="host"):
        build_targets(TargetForm(ssh=True, ssh_host="  "))


def test_container_without_arch_is_rejected():
    with pytest.raises(TargetFormError, match="arch"):
        build_targets(TargetForm(container=True, container_arches=[]))


def test_container_without_image_is_rejected():
    with pytest.raises(TargetFormError, match="image"):
        build_targets(TargetForm(container=True, container_images=" , "))


def test_nothing_selected_is_rejected():
    with pytest.raises(TargetFormError, match="target"):
        build_targets(TargetForm())


def test_inventory_selection_by_index():
    inventory = [Target(kind="ssh", name="a"), Target(kind="ssh", name="b")]

    targets = build_targets(TargetForm(inventory_selected=[1]), inventory)

    assert targets == [inventory[1]]


def test_inventory_selection_out_of_range_selects_nothing():
    inventory = [Target(kind="ssh", name="a")]

    with pytest.raises(TargetFormError):
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
    assert row.answers_text == "Linux Debian 11; x"
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
    assert row.answers_text == ""


def test_result_row_no_answers_is_ok_but_says_so():
    row = ResultRow.from_result(
        ClientRelevanceResult(host="h", transport="ssh", client_relevance="nothing")
    )

    assert row.ok is True
    assert row.answers_text == "(no answers)"


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
        (Target(kind="ssh", name="win", platform="windows"), "windows"),
        (Target(kind="ssh", name="pi", platform="raspbian"), "debian"),
        (Target(kind="ssh", name="sles", platform="suse"), None),
        (Target(kind="ssh", name="unknown"), None),
        (container("debian:11"), "debian"),
        (container("ubuntu:24.04"), "ubuntu"),
        (container("almalinux:9"), "rhel"),
        (container("amazonlinux:2023"), "rhel"),
        (container("oraclelinux:9"), "rhel"),
        (container("registry.access.redhat.com/ubi9/ubi"), "rhel"),
        (container("opensuse/leap:15"), None),
        (container("mystery:1"), None),
        # An explicit platform beats the image name.
        (
            Target(kind="container", name="x", image="debian:11", platform="ubuntu"),
            "ubuntu",
        ),
    ],
)
def test_target_platform(target, platform):
    assert target_platform(target) == platform


@pytest.mark.parametrize(
    ("sys_platform", "platform"), [("darwin", "macos"), ("win32", "windows"), ("linux", None)]
)
def test_local_target_platform_is_this_machine(monkeypatch, sys_platform, platform):
    monkeypatch.setattr("sys.platform", sys_platform)

    assert target_platform(Target(kind="local", name="local")) == platform


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
    with pytest.raises(TargetFormError):
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
