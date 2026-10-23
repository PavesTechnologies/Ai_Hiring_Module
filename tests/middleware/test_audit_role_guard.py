"""
Audit entries record the role that authorised the action - never the raw first
entry on the sign-in, which for some tokens is an org label ("General").

  - a static guard over app/: no `...roles[0]` anywhere in code;
  - every route that calls authorising_role(user) without naming the roles gets
    `user` from require_roles, so the roles the route allowed are on it;
  - require_roles / authorising_role behaviour, with the request mocked.
No database, no email.
"""
import ast
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from app.middleware.rbac import ROLE_PRECEDENCE, TokenUser, authorising_role, require_roles
from app.models.identity import UserRole

APP = Path(__file__).resolve().parents[2] / "app"
ROUTES = APP / "api" / "routes"

# Routes with no role check at all (any signed-in user): authorising_role falls
# back to the broadest role the user holds. Listed so a new one is a decision.
NO_ROLE_CHECK = {("campaign_candidate.py", "create_campaign_candidate")}


def _raw_first_role_uses(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))     # some files carry a BOM
    for node in ast.walk(tree):
        if not isinstance(node, ast.Subscript):
            continue
        index = node.slice
        if not (isinstance(index, ast.Constant) and index.value == 0):
            continue
        target = node.value
        name = target.attr if isinstance(target, ast.Attribute) else getattr(target, "id", "")
        if name.endswith("roles"):
            yield f"{path.relative_to(APP.parent)}:{node.lineno}"


def test_no_code_records_the_raw_first_role():
    offenders = [hit for path in sorted(APP.rglob("*.py")) for hit in _raw_first_role_uses(path)]
    assert offenders == [], (
        "These take the first role on the sign-in (can be 'General'); use authorising_role(...) "
        f"from app.middleware.rbac instead: {offenders}")


def test_the_guard_catches_the_raw_first_role(tmp_path):
    sample = tmp_path / "sample.py"
    sample.write_text("a = user.roles[0] if user.roles else None\nb = actor_roles[0]\nc = items[0]\n")
    with patch(f"{__name__}.APP", tmp_path):
        assert sorted(hit.rsplit(":", 1)[1] for hit in _raw_first_role_uses(sample)) == ["1", "2"]


def _user_default(func: ast.FunctionDef, src: str) -> str | None:
    positional = func.args.args
    defaults = dict(zip([a.arg for a in positional][len(positional) - len(func.args.defaults):], func.args.defaults))
    defaults.update({a.arg: d for a, d in zip(func.args.kwonlyargs, func.args.kw_defaults) if d is not None})
    return ast.get_source_segment(src, defaults["user"]) if "user" in defaults else None


def _bare_authorising_role_calls(func: ast.FunctionDef) -> int:
    return sum(1 for node in ast.walk(func)
               if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "authorising_role"
               and len(node.args) == 1 and not node.keywords)


def test_routes_calling_authorising_role_without_roles_get_user_from_require_roles():
    problems = []
    for path in sorted(ROUTES.glob("*.py")):
        src = path.read_text(encoding="utf-8-sig")
        for func in ast.walk(ast.parse(src)):
            if not isinstance(func, ast.FunctionDef) or not _bare_authorising_role_calls(func):
                continue
            if (path.name, func.name) in NO_ROLE_CHECK:
                continue
            default = _user_default(func, src) or ""
            if "require_roles" not in default:
                problems.append(f"{path.name}:{func.name} ({default or 'no user dependency'})")
    assert problems == [], (
        "authorising_role(user) only knows the route's roles when `user` comes from require_roles; "
        f"pass the roles explicitly, e.g. authorising_role(user, (UserRole.HR_ADMIN,)): {problems}")


# --- behaviour ---------------------------------------------------------------------------

def _check(roles, *allowed):
    request = SimpleNamespace(state=SimpleNamespace(token_payload={"user_id": "u-1", "email": "u@example.invalid",
                                                                   "roles": roles}))
    with patch("app.middleware.rbac._ensure_local_user"):
        return require_roles(*allowed)(request, db=MagicMock())


@pytest.mark.parametrize("roles, allowed, expected", [
    (["General", "RECRUITER"], (UserRole.HR_ADMIN, UserRole.RECRUITER), "RECRUITER"),
    (["General", "RECRUITER", "HR_ADMIN"], (UserRole.RECRUITER, UserRole.HR_ADMIN), "HR_ADMIN"),
    # HR_ADMIN is held but this route doesn't allow it: the role that authorised it is RECRUITER
    (["HR_ADMIN", "RECRUITER"], (UserRole.RECRUITER,), "RECRUITER"),
    (["General", "RECRUITER", "HIRING_MANAGER"], (UserRole.HIRING_MANAGER, UserRole.RECRUITER), "HIRING_MANAGER"),
])
def test_require_roles_carries_the_authorising_role(roles, allowed, expected):
    user = _check(roles, *allowed)
    assert user.roles == roles                                   # the sign-in itself is untouched
    assert authorising_role(user) == expected


def test_explicit_roles_win_in_the_order_given():
    user = _check(["General", "HR_ADMIN", "RECRUITER"], UserRole.HR_ADMIN, UserRole.RECRUITER)
    assert authorising_role(user, (UserRole.RECRUITER, UserRole.HR_ADMIN)) == "RECRUITER"


def test_without_require_roles_it_falls_back_to_the_broadest_held_role():
    user = TokenUser(user_id="u-1", email="u@example.invalid", roles=["General", "RECRUITER", "HIRING_MANAGER"])
    assert authorising_role(user) == "HIRING_MANAGER"
    assert ROLE_PRECEDENCE.index("HR_ADMIN") < ROLE_PRECEDENCE.index("HIRING_MANAGER") < ROLE_PRECEDENCE.index(
        "RECRUITER")


def test_never_an_org_label():
    assert authorising_role(["General"]) is None
    assert authorising_role(TokenUser(user_id="u", email="", roles=["General"])) is None
    assert authorising_role(None, (UserRole.HR_ADMIN,)) is None


def test_routes_never_record_resolve_actor_role():
    """resolve_actor_role picks the first recognised role, not one the route allowed - routes use authorising_role."""
    offenders = [f"{path.name}:{node.lineno}"
                 for path in sorted(ROUTES.glob("*.py"))
                 for node in ast.walk(ast.parse(path.read_text(encoding="utf-8-sig")))
                 if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "resolve_actor_role"]
    assert offenders == [], f"use authorising_role(user) / authorising_roles(user) instead: {offenders}"
