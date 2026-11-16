"""SPEC ground rule 6: who may import whom, read from every module's imports.

Imports inside functions count too: a lazy import is still a dependency.
"""

from __future__ import annotations

import ast
from pathlib import Path

import task_orchard

ROOT = Path(task_orchard.__file__).parent


def _module_name(path: Path) -> str:
    parts = path.relative_to(ROOT.parent).with_suffix("").parts
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    package = _module_name(path).rsplit(".", 1)[0]
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # relative: resolve against this module's package
                base = package.rsplit(".", node.level - 1)[0] if node.level > 1 else package
                found.add(f"{base}.{node.module}" if node.module else base)
            elif node.module:
                found.add(node.module)
    return found


MODULES = {
    _module_name(path): _imports(path)
    for path in ROOT.rglob("*.py")
    if "migrations" not in path.parts
}


def _within(name: str, prefix: str) -> bool:
    return name == prefix or name.startswith(prefix + ".")


def _offenders(importer: str, forbidden: list[str]) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for module, imports in MODULES.items():
        if not _within(module, importer):
            continue
        bad = {i for i in imports if any(_within(i, f) for f in forbidden)}
        if bad:
            out[module] = bad
    return out


def test_the_modules_were_found() -> None:
    assert {"task_orchard.core.service", "task_orchard.tools", "task_orchard.agent.loop"} <= set(
        MODULES
    )


def test_core_imports_nothing_above_it() -> None:
    above = ["task_orchard.tools", "task_orchard.agent", "task_orchard.mcp", "task_orchard.api"]
    assert _offenders("task_orchard.core", above) == {}


def test_tools_imports_only_core() -> None:
    ours = {i for i in MODULES["task_orchard.tools"] if _within(i, "task_orchard")}
    assert all(_within(i, "task_orchard.core") for i in ours), ours


def test_agent_and_mcp_do_not_import_each_other() -> None:
    assert _offenders("task_orchard.agent", ["task_orchard.mcp", "task_orchard.api"]) == {}
    assert _offenders("task_orchard.mcp", ["task_orchard.agent", "task_orchard.api"]) == {}


def test_only_the_model_module_imports_anthropic() -> None:
    importers = {
        m for m, imports in MODULES.items() if any(_within(i, "anthropic") for i in imports)
    }
    assert importers == {"task_orchard.agent.model"}
