"""Regression: a function-local import must not shadow a module-level name
that the same function also reads on a path the import does not dominate.

2026-09-16 (commit 2c83081): the anthropic_direct primary branch imported
``prepaid_vision_over_cap`` locally inside ``chat_completions``. Python then
treats the name as local for the WHOLE function, so the pre-existing vision
path (module-level import, different ``if`` branch) raised
``UnboundLocalError: cannot access local variable 'prepaid_vision_over_cap'``
on every image request — prod vision was dead until the energy wizard refused
a start for a customer.

Rule: for every function-local ``from x import name`` whose ``name`` also
exists at module level, every load of ``name`` inside that function must sit
in the same statement block at or after the import (the only region where the
import is guaranteed to have run). Anything else is a latent UnboundLocalError.
"""
import ast
from pathlib import Path

MAIN = Path(__file__).resolve().parents[2] / "src" / "main.py"
BODY_FIELDS = ("body", "orelse", "finalbody", "handlers")


def _module_level_imports(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.ImportFrom):
            names.update(a.asname or a.name for a in node.names)
        elif isinstance(node, ast.Import):
            names.update((a.asname or a.name).split(".")[0] for a in node.names)
    return names


def _stmt_lists(fn: ast.AST):
    """Yield every statement list (body/orelse/...) inside fn, excluding nested defs."""
    stack = [fn]
    while stack:
        node = stack.pop()
        for field in BODY_FIELDS:
            lst = getattr(node, field, None)
            if isinstance(lst, list) and lst and isinstance(lst[0], ast.stmt):
                yield lst
                for child in lst:
                    if not isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                        stack.append(child)
            elif isinstance(lst, list):
                for child in lst:  # ExceptHandler list
                    stack.append(child)


def _loads(node: ast.AST, name: str):
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name) and sub.id == name and isinstance(sub.ctx, ast.Load):
            yield sub


def test_no_function_local_import_shadows_module_level_name():
    tree = ast.parse(MAIN.read_text())
    top = _module_level_imports(tree)
    offenders: list[str] = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        # name -> (import lines, ids of loads dominated by SOME local import)
        local: dict[str, tuple[list[int], set[int]]] = {}
        for lst in _stmt_lists(fn):
            for idx, stmt in enumerate(lst):
                if not isinstance(stmt, ast.ImportFrom):
                    continue
                for a in stmt.names:
                    name = a.asname or a.name
                    if name not in top:
                        continue
                    lines, covered = local.setdefault(name, ([], set()))
                    lines.append(stmt.lineno)
                    covered.update(id(n) for s in lst[idx:] for n in _loads(s, name))
        for name, (lines, covered) in local.items():
            for load in _loads(fn, name):
                if id(load) not in covered:
                    offenders.append(
                        f"{fn.name}: '{name}' is imported locally (lines {lines}) but read at "
                        f"line {load.lineno} on a path no local import dominates → UnboundLocalError"
                    )
    assert not offenders, "\n".join(sorted(set(offenders)))
