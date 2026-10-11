"""Repo config that has to agree across files, so CI's gates can't contradict each other."""

from __future__ import annotations

import datetime
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


# jgstew's own libraries: this app is developed alongside them and often
# needs a release the day it ships, so they skip the cooldown.
JGSTEW_PACKAGES = {"besapi", "bigfix-relevance-analyzer", "bigfix-remote-client-relevance"}


def uv_settings() -> dict:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["tool"]["uv"]


def test_dependencies_have_a_seven_day_cooldown():
    assert uv_settings()["exclude-newer"] == "7 days"


# Temporary exemptions, each until its pinned floor is 7 days old: the
# cooldown came in after these floors were raised to releases from that week.
# Remove the entry (here and in pyproject.toml) once its date has passed.
TEMPORARY = {
    "nicegui": datetime.date(2026, 10, 14),  # nicegui[native]>=3.18.0, out 2026-10-07
    "ruff": datetime.date(2026, 10, 16),  # ruff>=0.17.0, out 2026-10-09
}


def test_jgstew_packages_and_only_they_skip_the_cooldown():
    exempt = uv_settings()["exclude-newer-package"]
    assert exempt == dict.fromkeys(JGSTEW_PACKAGES | set(TEMPORARY), "0 days")


def test_no_temporary_exemption_outlives_its_date():
    expired = sorted(name for name, until in TEMPORARY.items() if datetime.date.today() > until)
    assert not expired, (
        f"cooldown exemptions past their date: {expired}; remove them from "
        "exclude-newer-package in pyproject.toml and from TEMPORARY here, then `uv lock`"
    )


def test_every_jgstew_package_is_a_dependency():
    # a library dropped from the dependencies shouldn't keep its exemption
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    names = {re.split(r"[\[<>=!~; ]", d, maxsplit=1)[0] for d in project["dependencies"]}
    assert names >= JGSTEW_PACKAGES
