"""The release's PyPI pre-check (.github/scripts/pypi_precheck.py), offline: urlopen is faked."""

from __future__ import annotations

import email.message
import hashlib
import importlib.util
import io
import json
import logging
import urllib.error
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[2] / ".github" / "scripts" / "pypi_precheck.py"
_spec = importlib.util.spec_from_file_location("pypi_precheck", SCRIPT)
assert _spec and _spec.loader
precheck = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(precheck)

PACKAGE = "bigfix-relevance-debugger"
SIMPLE = f"https://pypi.org/simple/{PACKAGE}/"
PROJECT = f"https://pypi.org/pypi/{PACKAGE}/json"
SIMPLE_TYPE = "application/vnd.pypi.simple.v1+json"


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class FakeResponse(io.BytesIO):
    def __init__(self, body: bytes, content_type: str):
        super().__init__(body)
        self.headers = email.message.Message()
        self.headers["Content-Type"] = content_type


def fake_urlopen(responses: dict[str, object]):
    """Each URL maps to (body, content type), an int HTTP status to raise, or an exception."""

    def urlopen(request, timeout=None):
        url = request.full_url if hasattr(request, "full_url") else request
        answer = responses[url]
        if isinstance(answer, int):
            raise urllib.error.HTTPError(url, answer, "error", email.message.Message(), None)
        if isinstance(answer, Exception):
            raise answer
        body, content_type = answer  # type: ignore[misc]
        return FakeResponse(body, content_type)

    return urlopen


def simple(files: dict[str, str]) -> tuple[bytes, str]:
    body = {"files": [{"filename": n, "hashes": {"sha256": h}} for n, h in files.items()]}
    return json.dumps(body).encode(), SIMPLE_TYPE


def project(files: dict[str, str], version: str = "0.1.0") -> tuple[bytes, str]:
    body = {
        "releases": {version: [{"filename": n, "digests": {"sha256": h}} for n, h in files.items()]}
    }
    return json.dumps(body).encode(), "application/json"


@pytest.fixture(autouse=True)
def _capture_info(caplog):
    caplog.set_level(logging.INFO, logger="pypi_precheck")


@pytest.fixture
def dist(tmp_path: Path) -> Path:
    d = tmp_path / "dist"
    d.mkdir()
    (d / "bigfix_relevance_debugger-0.1.0-py3-none-any.whl").write_bytes(b"wheel")
    (d / "bigfix_relevance_debugger-0.1.0.tar.gz").write_bytes(b"sdist")
    (d / "SHA256SUMS.txt").write_text("not a dist file")
    return d


WHL = "bigfix_relevance_debugger-0.1.0-py3-none-any.whl"
SDIST = "bigfix_relevance_debugger-0.1.0.tar.gz"


def run(dist: Path, responses: dict[str, object], version: str = "0.1.0") -> int:
    return precheck.main(dist, PACKAGE, version, urlopen=fake_urlopen(responses))


def test_identical_files_on_pypi_pass(dist, caplog):
    files = {WHL: sha(b"wheel"), SDIST: sha(b"sdist")}
    assert run(dist, {SIMPLE: simple(files), PROJECT: project(files)}) == 0
    assert "already on PyPI (identical)" in caplog.text


def test_a_differing_file_fails_before_the_draft_is_touched(dist, caplog):
    files = {WHL: sha(b"wheel"), SDIST: sha(b"other sdist")}
    assert run(dist, {SIMPLE: simple(files), PROJECT: project(files)}) == 1
    assert f"::error::PyPI already has {SDIST}" in caplog.text


def test_a_file_missing_from_one_source_still_counts(dist, caplog):
    # the simple index is stale and misses the sdist; the project JSON has it
    stale = {WHL: sha(b"wheel")}
    fresh = {WHL: sha(b"wheel"), SDIST: sha(b"other sdist")}
    assert run(dist, {SIMPLE: simple(stale), PROJECT: project(fresh)}) == 1
    # and the other way round
    assert run(dist, {SIMPLE: simple(fresh), PROJECT: project(stale)}) == 1
    assert "::error::" in caplog.text


def test_a_cached_404_on_one_source_is_covered_by_the_other(dist):
    fresh = {WHL: sha(b"wheel"), SDIST: sha(b"other sdist")}
    assert run(dist, {SIMPLE: 404, PROJECT: project(fresh)}) == 1
    assert run(dist, {SIMPLE: simple(fresh), PROJECT: 404}) == 1


def test_an_unreleased_project_passes(dist, caplog):
    assert run(dist, {SIMPLE: 404, PROJECT: 404}) == 0
    assert "not on PyPI yet" in caplog.text


def test_an_unreleased_version_passes(dist):
    other = {"bigfix_relevance_debugger-0.0.9.tar.gz": sha(b"old")}
    assert run(dist, {SIMPLE: simple(other), PROJECT: project(other, "0.0.9")}) == 0


def test_dist_without_this_version_fails(dist, caplog):
    assert run(dist, {SIMPLE: 404, PROJECT: 404}, version="0.2.0") == 1
    assert "::error::dist/ has no wheel and sdist for 0.2.0" in caplog.text


def test_other_http_errors_fail_with_a_clear_error(dist, caplog):
    assert run(dist, {SIMPLE: 503, PROJECT: project({})}) == 1
    assert "::error::" in caplog.text and "503" in caplog.text


def test_network_errors_fail_with_a_clear_error(dist, caplog):
    down = urllib.error.URLError("timed out")
    assert run(dist, {SIMPLE: down, PROJECT: project({})}) == 1
    assert "::error::" in caplog.text and "timed out" in caplog.text


def test_an_html_simple_index_fails_with_a_clear_error(dist, caplog):
    html = (b"<html>files</html>", "text/html")
    assert run(dist, {SIMPLE: html, PROJECT: project({})}) == 1
    assert "::error::" in caplog.text and "text/html" in caplog.text


@pytest.mark.parametrize(
    "body",
    [b"not json", b'{"no": "files"}', b'{"files": [{"hashes": {}}]}'],
)
def test_a_malformed_simple_index_fails_with_a_clear_error(dist, caplog, body):
    assert run(dist, {SIMPLE: (body, SIMPLE_TYPE), PROJECT: project({})}) == 1
    assert "::error::" in caplog.text


def test_a_malformed_project_json_fails_with_a_clear_error(dist, caplog):
    assert run(dist, {SIMPLE: simple({}), PROJECT: (b'{"no": 1}', "application/json")}) == 1
    assert "::error::" in caplog.text


def test_a_simple_entry_without_sha256_is_left_to_the_project_json(dist):
    # PEP 691 allows an empty `hashes`; such an entry can't be compared
    body = json.dumps({"files": [{"filename": SDIST, "hashes": {}}]}).encode()
    files = {WHL: sha(b"wheel"), SDIST: sha(b"sdist")}
    assert run(dist, {SIMPLE: (body, SIMPLE_TYPE), PROJECT: project(files)}) == 0
