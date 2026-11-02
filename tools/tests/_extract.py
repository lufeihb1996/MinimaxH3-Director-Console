"""Test helper: execute selected top-level definitions from backend modules inside one patchable namespace.

Lets offline tests replace collaborators (db, hub, get_shot ...) by editing the dict, without importing
the real app (which would initialise the production database).
"""
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def extract(ns: dict, sources: dict[str, set[str]]) -> dict:
    """sources: {'backend/services/video.py': {'run_shot', ...}} -> exec those nodes into ns."""
    for rel, names in sources.items():
        path = ROOT / rel
        tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        selected = []
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name in names:
                node.decorator_list = []
                selected.append(node)
            elif isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id in names for t in node.targets):
                selected.append(node)
        found = {getattr(n, "name", None) or n.targets[0].id for n in selected}
        missing = set(names) - found
        if missing:
            raise AssertionError(f"{rel} 缺少定义: {sorted(missing)}")
        exec(compile(ast.Module(body=selected, type_ignores=[]), str(path), "exec"), ns)
    return ns
