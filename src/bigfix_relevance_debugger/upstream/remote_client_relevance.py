"""Candidates for bigfix-remote-client-relevance.

See ``upstream/__init__.py`` for the rules. Each function notes where it
would live upstream.
"""

from __future__ import annotations

import re
import sys
from collections.abc import Callable
from pathlib import Path

from bigfix_remote_client_relevance import Target, find_inventory_path
from bigfix_remote_client_relevance.bootstrap.targets import KNOWN_TARGETS, host_arch
from bigfix_remote_client_relevance.discovery import (
    ImageSource,
    run_auto_discovery,
)
from bigfix_remote_client_relevance.inventory_paths import user_inventory_path

# The arches qna ships builds for, in display order. discovery.SUPPORTED_ARCHS
# holds the same tuple but isn't in that module's __all__; use it once it is.
SUPPORTED_ARCHS = ("x86_64", "arm64")

# Image-name tokens -> KNOWN_TARGETS key. Upstream this would merge with
# discovery._FAMILY_OF_DISTRO (private), which maps the same names to a
# family for discovery's own use.
_IMAGE_TOKEN_PLATFORMS = {
    "debian": "debian",
    "raspbian": "raspbian",
    "ubuntu": "ubuntu",
    "rhel": "rhel",
    "ubi": "rhel",
    "ubi8": "rhel",
    "ubi9": "rhel",
    "ubi10": "rhel",
    "redhat": "rhel",
    "fedora": "rhel",
    "centos": "rhel",
    "almalinux": "rhel",
    "rockylinux": "rhel",
    "oraclelinux": "rhel",
    "amazonlinux": "rhel",
    "opensuse": "suse",
    "suse": "suse",
    "sles": "suse",
}

_SYS_PLATFORMS = {"darwin": "macos", "win32": "windows"}


def target_platform(target: Target) -> str | None:
    """The ``KNOWN_TARGETS`` key a target will run as, if knowable without connecting.

    An explicit (or inventory-recorded) ``platform`` wins; otherwise a
    container is read from its image name and ``local`` from this machine.
    None means unknown: an SSH host never probed, or a Linux controller (its
    distro needs the os-release probe the transport already does).

    Upstream: a ``Target.known_platform`` property, or a helper next to
    ``classify_uname`` in ``bootstrap/targets.py``.
    """
    if target.platform:
        return target.platform
    if target.kind == "container":
        image = (target.image or target.name).lower()
        for token in re.split(r"[/:._@-]", image):
            platform = _IMAGE_TOKEN_PLATFORMS.get(token)
            if platform in KNOWN_TARGETS:
                return platform
        return None
    if target.kind == "local":
        return _SYS_PLATFORMS.get(sys.platform)
    return None


def published_arches(image: str, images: ImageSource | None) -> list[str]:
    """The arches to run ``image`` as: every one its manifest publishes that qna supports.

    Falls back to this machine's arch when there is no engine or the registry
    can't be asked (offline, a local-only tag) -- the same rule discovery
    applies to its container candidates. Blocking: it may ask a registry.

    Upstream: ``discovery.py``, beside ``EngineImageSource``.
    """
    published = images.platforms(image) if images is not None else None
    if not published:
        return [host_arch()]
    # EngineImageSource already filters to what qna supports; SUPPORTED_ARCHS
    # only orders the result, so an arch the library adds later still comes through.
    known = [arch for arch in SUPPORTED_ARCHS if arch in published]
    return known + sorted(published - set(SUPPORTED_ARCHS))


def find_or_discover_inventory(
    *,
    find: Callable[[], Path | None] | None = None,
    user_path: Path | None = None,
    discover: Callable[[Path], list[str]] | None = None,
) -> Path | None:
    """The inventory to use, running first-run auto-discovery when there is none.

    Same rule as the CLI: discover only when the search path finds nothing and
    the per-user file doesn't exist yet. Discovery always leaves that file
    behind (working hosts, or a placeholder when none worked), so it never
    repeats on its own. Blocking -- discovery evaluates every candidate, may
    pull images, and calls ``asyncio.run`` -- so call it from a worker thread
    inside an event loop.

    Upstream: the body of ``cli.py``'s first-run branch, so the CLI and other
    front ends share it.
    """
    # Defaults resolved per call, not bound at import, so they can be replaced.
    found = (find or find_inventory_path)()
    if found is not None:
        return found
    user_path = user_path if user_path is not None else user_inventory_path()
    if not user_path.exists():
        (discover or run_auto_discovery)(user_path)
    return user_path if user_path.is_file() else None


def discover_into(
    path: Path | None = None,
    *,
    discover: Callable[[Path], list[str]] | None = None,
) -> Path | None:
    """Run auto-discovery into ``path`` (the per-user inventory by default).

    Unlike :func:`find_or_discover_inventory`, it runs even when the file
    exists: for a front end whose user has no client targets configured
    (an inventory with no hosts, such as the placeholder a fruitless
    discovery leaves). ``run_auto_discovery`` only adds hosts missing from
    the file. Blocking, like discovery itself. Returns ``path`` when it is a
    file afterwards.

    Upstream: ``discovery.py``, beside ``run_auto_discovery``.
    """
    path = path if path is not None else user_inventory_path()
    (discover or run_auto_discovery)(path)
    return path if path.is_file() else None
