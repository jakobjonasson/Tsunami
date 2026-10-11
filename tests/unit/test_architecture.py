"""Structural rules from design.md, Build plan: Rules for the build."""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import tsunami

PACKAGE = Path(tsunami.__file__).parent
CORE = PACKAGE / "core"

# core/ must stay importable headless: no Qt, no MCP SDK, no front ends.
FORBIDDEN_IN_CORE = ("PySide6", "mcp", "tsunami.gui", "tsunami.mcp_server")

# Top-level packages of dependencies. A module of ours with one of these names (or a
# stdlib name) can shadow the real package when run as a script or frozen.
DEPENDENCY_PACKAGES = {
    "PySide6",
    "netmiko",
    "paramiko",
    "serial",
    "jinja2",
    "pydantic",
    "ruamel",
    "keyring",
    "mcp",
}


def _absolute_imports(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name, node.lineno
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            yield node.module, node.lineno


def _is_under(name: str, prefix: str) -> bool:
    return name == prefix or name.startswith(prefix + ".")


def test_core_has_no_front_end_imports():
    bad = [
        f"{path.relative_to(PACKAGE)}:{line}: {name}"
        for path in sorted(CORE.rglob("*.py"))
        for name, line in _absolute_imports(path)
        if any(_is_under(name, prefix) for prefix in FORBIDDEN_IN_CORE)
    ]
    assert not bad, "forbidden imports in core/:\n" + "\n".join(bad)


def test_no_module_shadows_stdlib_or_dependency():
    reserved = set(sys.stdlib_module_names) | DEPENDENCY_PACKAGES
    bad = []
    for path in sorted(PACKAGE.rglob("*")):
        if "__pycache__" in path.parts:
            continue
        name = path.name if path.is_dir() else path.stem if path.suffix == ".py" else None
        if name and not name.startswith("__") and name in reserved:
            bad.append(str(path.relative_to(PACKAGE)))
    assert not bad, "module names that shadow other packages:\n" + "\n".join(bad)
