"""No debug surfaces in the app: the /test RBAC routes are gone and the JWT middleware prints nothing."""
import ast
from pathlib import Path

from app.main import app

ROOT = Path(__file__).resolve().parents[2]


def test_no_test_routes_are_registered():
    paths = [getattr(route, "path", "") for route in app.routes]
    assert not [p for p in paths if p == "/test" or p.startswith("/test/")]


def test_the_jwt_middleware_never_prints():
    tree = ast.parse((ROOT / "app" / "middleware" / "jwt_middleware.py").read_text(encoding="utf-8"))
    calls = [node for node in ast.walk(tree)
             if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "print"]
    assert not calls
