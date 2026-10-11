"""Stop a release before it touches its draft if PyPI already has different bytes.

PyPI never replaces a file. If an earlier attempt already uploaded this
version from other bytes (a newer commit on main, or a rebuild), the release
can't finish, and `uv publish --check-url` would only notice after the draft
release's assets had been replaced. Files PyPI doesn't have yet, or has with
the same sha256, are fine: that's a re-run finishing a partial release.

PyPI is asked twice and the answers are merged, so a file is only missed if
both are stale at once: the PEP 691 JSON simple index (what `uv publish
--check-url` reads) and the project JSON. Both go through PyPI's CDN, which
can keep serving an old answer, e.g. a 404 primed before the upload, for a
while; on the 0.1.0 release the per-version JSON did exactly that while the
project JSON was already current.

Run by .github/workflows/tag_and_release.yaml from the repo root:
    python .github/scripts/pypi_precheck.py <package> <version>
Tested offline in tests/unit/test_pypi_precheck.py.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sys
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

logger = logging.getLogger("pypi_precheck")

SIMPLE_TYPE = "application/vnd.pypi.simple.v1+json"


class PrecheckError(Exception):
    """A reason to stop the release, shown as a GitHub ::error:: annotation."""


def _fetch_json(urlopen: Callable[..., Any], url: str, accept: str) -> Any | None:
    """The parsed body, or None for a 404 (nothing released yet)."""
    request = urllib.request.Request(url, headers={"Accept": accept})
    try:
        with urlopen(request, timeout=30) as response:
            content_type = response.headers.get_content_type()
            if content_type != accept:
                raise PrecheckError(f"{url} answered {content_type}, expected {accept}")
            body = response.read()
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise PrecheckError(f"{url} answered HTTP {e.code}") from e
    except urllib.error.URLError as e:
        raise PrecheckError(f"couldn't reach {url}: {e.reason}") from e
    try:
        return json.loads(body)
    except ValueError as e:
        raise PrecheckError(f"{url} didn't return valid JSON") from e


def released_hashes(package: str, urlopen: Callable[..., Any]) -> dict[str, set[str]]:
    """Every file PyPI lists for the package, any version, with its sha256(s)."""
    released: dict[str, set[str]] = {}

    simple_url = f"https://pypi.org/simple/{package}/"
    simple = _fetch_json(urlopen, simple_url, SIMPLE_TYPE)
    try:
        for f in simple["files"] if simple is not None else []:
            name, hashes = f["filename"], f["hashes"]
            # PEP 691 allows an empty `hashes`; the project JSON covers those
            if sha256 := hashes.get("sha256"):
                released.setdefault(name, set()).add(sha256)
    except (KeyError, TypeError, AttributeError) as e:
        raise PrecheckError(f"unexpected simple-index response from {simple_url}") from e

    project_url = f"https://pypi.org/pypi/{package}/json"
    project = _fetch_json(urlopen, project_url, "application/json")
    try:
        for files in project["releases"].values() if project is not None else []:
            for f in files:
                released.setdefault(f["filename"], set()).add(f["digests"]["sha256"])
    except (KeyError, TypeError, AttributeError) as e:
        raise PrecheckError(f"unexpected project JSON from {project_url}") from e

    return released


def local_hashes(dist: Path, package: str, version: str) -> dict[str, str]:
    """This version's wheel and sdist in dist/, by sha256."""
    stem = package.replace("-", "_") + "-" + version
    local = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in dist.glob(f"{stem}[-.]*")}
    if not any(n.endswith(".whl") for n in local) or not any(n.endswith(".tar.gz") for n in local):
        raise PrecheckError(f"dist/ has no wheel and sdist for {version}: {sorted(local)}")
    return local


def main(
    dist: Path,
    package: str,
    version: str,
    urlopen: Callable[..., Any] = urllib.request.urlopen,
) -> int:
    try:
        local = local_hashes(dist, package, version)
        released = released_hashes(package, urlopen)
    except PrecheckError as e:
        logger.error("::error::%s", e)
        return 1
    differ = sorted(n for n, h in local.items() if n in released and h not in released[n])
    if differ:
        logger.error(
            "::error::PyPI already has %s for %s with different bytes. PyPI never replaces "
            "a file: bump the version, or re-run the original run's failed jobs.",
            ", ".join(differ),
            version,
        )
        return 1
    for name in sorted(local):
        state = "already on PyPI (identical)" if name in released else "not on PyPI yet"
        logger.info("%s: %s", name, state)
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stdout)
    sys.exit(main(Path("dist"), sys.argv[1], sys.argv[2]))
