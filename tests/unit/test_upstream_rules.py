"""Keep upstream/ donatable: each module may import only the stdlib and its own library."""

from __future__ import annotations

import ast
import importlib.util
import sys
from pathlib import Path

import pytest

UPSTREAM = Path(__file__).parents[2] / "src" / "bigfix_relevance_debugger" / "upstream"

ALLOWED = {
    "remote_client_relevance.py": "bigfix_remote_client_relevance",
    "relevance_analyzer.py": "bigfix_relevance_analyzer",
}
"""The library each module may donate to. A module exists only while it has
something not yet shipped, so a rule can outlive its module."""


def present(upstream: Path) -> dict[str, str]:
    """The modules under ``upstream`` that have a rule, with their library."""
    return {m: lib for m, lib in ALLOWED.items() if (upstream / m).exists()}


PRESENT = present(UPSTREAM)


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


def modules_in(upstream: Path) -> set[str]:
    return {p.name for p in upstream.glob("*.py") if p.name != "__init__.py"}


def test_every_upstream_module_has_a_rule():
    assert modules_in(UPSTREAM) <= set(ALLOWED)


def test_the_package_is_where_the_rules_look():
    """With no module left the per-module tests skip; make sure that's because there are none.

    A wrong ``UPSTREAM`` would also find no modules and skip every check.
    """
    assert (UPSTREAM / "__init__.py").is_file()
    assert PRESENT == present(UPSTREAM)
    assert set(PRESENT) == modules_in(UPSTREAM)


def test_a_new_module_is_checked(tmp_path):
    """``present`` (what the per-module tests are parametrized from) picks up a module added later."""
    (tmp_path / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "remote_client_relevance.py").write_text("", encoding="utf-8")
    (tmp_path / "stray.py").write_text("", encoding="utf-8")

    assert present(tmp_path) == {"remote_client_relevance.py": "bigfix_remote_client_relevance"}
    assert modules_in(tmp_path) - set(ALLOWED) == {"stray.py"}


@pytest.mark.parametrize(("module", "library"), sorted(PRESENT.items()))
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


def public_names(tree: ast.Module) -> set[str]:
    """The public names a module defines at top level: defs, classes, assignments."""
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            names.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return {name for name in names if not name.startswith("_")}


def exported_names(tree: ast.Module) -> set[str]:
    """A library module's public API: its ``__all__``, else every public name."""
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target, value = node.targets[0], node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            target, value = node.target, node.value
        else:
            continue
        if isinstance(target, ast.Name) and target.id == "__all__":
            return set(ast.literal_eval(value))
    return public_names(tree)


def library_api(root: Path) -> set[str]:
    """Every name some module under ``root`` exports, read from source.

    Not imported: a library module may import only on one OS. A name outside a module's ``__all__`` is that
    library's private business, so ours may share it.
    """
    return set().union(
        *(exported_names(ast.parse(p.read_text(encoding="utf-8"))) for p in root.rglob("*.py"))
    )


@pytest.mark.parametrize(("module", "library"), sorted(PRESENT.items()))
def test_nothing_the_library_ships_is_kept_here(module, library):
    """Once the library has it, ours goes: callers use the library's."""
    spec = importlib.util.find_spec(library)
    assert spec is not None and spec.submodule_search_locations
    [root] = spec.submodule_search_locations
    ours = public_names(ast.parse((UPSTREAM / module).read_text(encoding="utf-8")))

    assert sorted(ours & library_api(Path(root))) == []


def test_library_api_catches_a_duplicate(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text(
        "__all__ = ['shipped']\ndef shipped(): ...\ndef unlisted(): ...\n", encoding="utf-8"
    )
    (tmp_path / "pkg" / "plain.py").write_text(
        "async def fetch(): ...\nLIMIT: int = 3\nNAMES = ()\nclass Thing: ...\n"
        "def _private(): ...\n",
        encoding="utf-8",
    )
    (tmp_path / "pkg" / "typed.py").write_text(
        "__all__: list[str] = []\ndef hidden(): ...\n", encoding="utf-8"
    )

    assert library_api(tmp_path / "pkg") == {"shipped", "fetch", "LIMIT", "NAMES", "Thing"}
