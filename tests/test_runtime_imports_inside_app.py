"""
Code in app/ runs in the API and in the Celery worker. Celery puts the working directory on sys.path only while it
loads the app and its task modules, so a later import of a top-level folder outside app/ (scripts/, tests/,
alembic/, ...) fails in the worker - a golden run did (2026-11-12). The one such import, the golden runner, goes
through golden_runs.golden_runner(), which adds the project root first; any other is refused here.
"""
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ALLOWED = {("app/services/settings/golden_runs.py", "scripts.golden.run")}     # inside golden_runner()


def test_app_code_imports_nothing_outside_app_except_through_golden_runner():
    local = {p.name for p in ROOT.iterdir() if p.is_dir() and not p.name.startswith((".", "_"))
             and p.name not in ("app", "venv", "node_modules")}
    found = set()
    for path in (ROOT / "app").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8-sig"))):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module]
            for name in names:
                if name.split(".")[0] in local:
                    found.add((path.relative_to(ROOT).as_posix(), name))
    assert found == ALLOWED, sorted(found - ALLOWED)
