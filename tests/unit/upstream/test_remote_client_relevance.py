"""Tests for upstream/remote_client_relevance.py.

Written to move into bigfix-remote-client-relevance's own suite together
with the module: only that library's public API and the stdlib are used.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from bigfix_remote_client_relevance import Target
from bigfix_remote_client_relevance.bootstrap.targets import KNOWN_TARGETS

from bigfix_relevance_debugger.upstream import remote_client_relevance as rcr
from bigfix_relevance_debugger.upstream.remote_client_relevance import (
    find_or_discover_inventory,
    published_arches,
    target_platform,
)


def container(image, **kwargs):
    return Target(kind="container", name=image, image=image, **kwargs)


# --- target_platform ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("target", "platform"),
    [
        (Target(kind="ssh", name="win", platform="windows"), "windows"),
        (Target(kind="ssh", name="pi", platform="raspbian"), "raspbian"),
        (Target(kind="ssh", name="sles", platform="suse"), "suse"),
        (Target(kind="ssh", name="unknown"), None),
        (container("debian:11"), "debian"),
        (container("ubuntu:24.04"), "ubuntu"),
        (container("almalinux:9"), "rhel"),
        (container("rockylinux:9"), "rhel"),
        (container("amazonlinux:2023"), "rhel"),
        (container("oraclelinux:9"), "rhel"),
        (container("fedora:41"), "rhel"),
        (container("registry.access.redhat.com/ubi9/ubi"), "rhel"),
        (container("registry.access.redhat.com/ubi10/ubi:latest"), "rhel"),
        (container("opensuse/leap:15"), "suse"),
        (container("mystery:1"), None),
        # An explicit platform beats the image name.
        (container("debian:11", platform="ubuntu"), "ubuntu"),
    ],
)
def test_target_platform(target, platform):
    assert target_platform(target) == platform


def test_target_platform_only_returns_known_bootstrap_keys():
    images = ["debian:11", "ubuntu:24.04", "almalinux:9", "opensuse/leap:15", "ubi9/ubi"]

    for image in images:
        assert target_platform(container(image)) in KNOWN_TARGETS


@pytest.mark.parametrize(
    ("sys_platform", "platform"), [("darwin", "macos"), ("win32", "windows"), ("linux", None)]
)
def test_local_target_platform_is_this_machine(monkeypatch, sys_platform, platform):
    monkeypatch.setattr("sys.platform", sys_platform)

    assert target_platform(Target(kind="local", name="local")) == platform


# --- published_arches --------------------------------------------------------------


class FakeImages:
    def __init__(self, platforms):
        self._platforms = platforms
        self.asked: list[str] = []

    def local_tags(self):
        return []

    def platforms(self, image):
        self.asked.append(image)
        return self._platforms


def test_published_arches_from_manifest_in_stable_order():
    assert published_arches("debian:11", FakeImages({"arm64", "x86_64"})) == ["x86_64", "arm64"]


def test_published_arches_single_arch_image():
    assert published_arches("x86-only:1", FakeImages({"x86_64"})) == ["x86_64"]


def test_published_arches_unknown_manifest_means_this_machine(monkeypatch):
    monkeypatch.setattr(rcr, "host_arch", lambda: "arm64")

    assert published_arches("local-only:dev", FakeImages(None)) == ["arm64"]


def test_published_arches_no_engine_means_this_machine(monkeypatch):
    monkeypatch.setattr(rcr, "host_arch", lambda: "x86_64")

    assert published_arches("debian:11", None) == ["x86_64"]


def test_published_arches_trusts_the_engine_filter_and_keeps_new_arches():
    # EngineImageSource.platforms already filters to what qna supports; a
    # second local filter would drop an arch the library starts returning.
    assert published_arches("debian:11", FakeImages({"riscv64", "arm64", "x86_64"})) == [
        "x86_64",
        "arm64",
        "riscv64",
    ]


# --- find_or_discover_inventory ----------------------------------------------------


def test_existing_inventory_is_used_without_discovery(tmp_path):
    found = tmp_path / "remote_clients.toml"
    found.write_text("", encoding="utf-8")

    path = find_or_discover_inventory(
        find=lambda: found,
        user_path=tmp_path / "user.toml",
        discover=lambda p: pytest.fail("should not discover"),
    )

    assert path == found


def test_first_run_discovers_into_the_user_inventory(tmp_path):
    user = tmp_path / ".bigfix" / "remote_clients.toml"
    calls: list[Path] = []

    def discover(path):
        calls.append(path)
        path.parent.mkdir(parents=True)
        path.write_text('[hosts.local]\ntransport = "local"\n', encoding="utf-8")
        return ["local"]

    path = find_or_discover_inventory(find=lambda: None, user_path=user, discover=discover)

    assert calls == [user]
    assert path == user


def test_existing_user_file_is_not_rediscovered(tmp_path):
    # e.g. the placeholder a fruitless discovery leaves behind: it exists, so
    # discovery must not repeat on every start.
    user = tmp_path / "remote_clients.toml"
    user.write_text("# placeholder\n", encoding="utf-8")

    path = find_or_discover_inventory(
        find=lambda: None,
        user_path=user,
        discover=lambda p: pytest.fail("should not discover"),
    )

    assert path == user


def test_discovery_that_writes_nothing_returns_none(tmp_path):
    path = find_or_discover_inventory(
        find=lambda: None, user_path=tmp_path / "x.toml", discover=lambda p: []
    )

    assert path is None
