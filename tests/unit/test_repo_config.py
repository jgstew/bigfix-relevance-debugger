"""Repo config that has to agree across files, so CI's gates can't contradict each other."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).parents[2]


def locked_version(package: str) -> str:
    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    return next(p["version"] for p in lock["package"] if p["name"] == package)


def pre_commit_rev_version(repo: str) -> str:
    """The `# vX.Y.Z` comment on a SHA-pinned hook repo's `rev:` line."""
    config = (ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8")
    match = re.search(
        rf"repo:\s*https://github\.com/{re.escape(repo)}\s*\n\s*rev:\s*\S+\s*#\s*v?(\S+)", config
    )
    assert match, f"no SHA-pinned rev with a version comment for {repo}"
    return match.group(1)


def test_pre_commit_ruff_matches_the_locked_ruff():
    # test.yaml runs `uv run ruff` (uv.lock) and pre-commit.yaml runs the
    # ruff-pre-commit hook; both gate a release, so a formatting difference
    # between two ruff versions would fail one of them whatever the code says.
    assert pre_commit_rev_version("astral-sh/ruff-pre-commit") == locked_version("ruff")
