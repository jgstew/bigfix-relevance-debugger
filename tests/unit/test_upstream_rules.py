"""Keep upstream/ donatable: each module may import only the stdlib and its own library."""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

UPSTREAM = Path(__file__).parents[2] / "src" / "bigfix_relevance_debugger" / "upstream"

ALLOWED = {
    "remote_client_relevance.py": "bigfix_remote_client_relevance",
    "relevance_analyzer.py": "bigfix_relevance_analyzer",
}


def top_level_imports(path: Path) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.level > 0:
            names.add("<relative>")
    return names


def test_every_upstream_module_has_a_rule():
    modules = {p.name for p in UPSTREAM.glob("*.py") if p.name != "__init__.py"}

    assert modules == set(ALLOWED)


@pytest.mark.parametrize(("module", "library"), sorted(ALLOWED.items()))
def test_upstream_module_imports_only_stdlib_and_its_library(module, library):
    imported = top_level_imports(UPSTREAM / module)

    foreign = {
        name
        for name in imported
        if name not in sys.stdlib_module_names and name not in {library, "__future__"}
    }
    assert foreign == set(), f"{module} must not import {sorted(foreign)}"


def test_rule_catches_a_violation(tmp_path):
    bad = tmp_path / "bad.py"
    bad.write_text(
        "from nicegui import ui\nfrom bigfix_relevance_debugger import analysis\nfrom . import x\n",
        encoding="utf-8",
    )

    assert top_level_imports(bad) == {"nicegui", "bigfix_relevance_debugger", "<relative>"}
