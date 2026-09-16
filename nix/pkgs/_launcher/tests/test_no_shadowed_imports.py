"""No function-local import may shadow a module-level one.

Python binds a name imported inside a function for that function's ENTIRE
body, not from the import statement onward. So a redundant local `import json`
in one branch turns every *other* branch's use of the module-level `json` into
an UnboundLocalError — code that reads correct, passes review, and fails only
when the un-importing branch is taken.

That is not hypothetical: `nixos.apply_host` carried exactly this, and the
branch it broke was the default path of `fleet deploy nixos apply host <name>`
without --ip. It survived because the other branch (--ip) does run the import,
so every test and every manual check that passed an explicit IP was green.

A local import is still fine when the module is NOT imported at the top level
(the lazy-import pattern the heavy back-ends use to stay sandbox-safe) — this
only rejects the shadowing case.
"""
from __future__ import annotations

import ast
import pathlib

import pytest

_PKG = pathlib.Path(__file__).resolve().parent.parent / "fleet_launcher"


def _module_level_names(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in tree.body:  # top level only — not nested
        if isinstance(node, ast.Import):
            names.update((a.asname or a.name.split(".")[0]) for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.update((a.asname or a.name) for a in node.names)
    return names


def _shadowing_imports(tree: ast.Module, top: set[str]) -> list[tuple[str, int]]:
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for inner in ast.walk(node):
            if inner is node or not isinstance(inner, (ast.Import, ast.ImportFrom)):
                continue
            for alias in inner.names:
                bound = alias.asname or (
                    alias.name.split(".")[0] if isinstance(inner, ast.Import)
                    else alias.name)
                if bound in top:
                    found.append((f"{node.name}: {bound}", inner.lineno))
    return found


@pytest.mark.parametrize(
    "path", sorted(_PKG.rglob("*.py")), ids=lambda p: str(p.relative_to(_PKG)))
def test_no_local_import_shadows_a_module_level_one(path: pathlib.Path):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    offenders = _shadowing_imports(tree, _module_level_names(tree))
    assert not offenders, (
        f"{path.relative_to(_PKG)} re-imports a module-level name inside a "
        f"function, making it local for that whole function: "
        + "; ".join(f"{what} (line {ln})" for what, ln in offenders))
