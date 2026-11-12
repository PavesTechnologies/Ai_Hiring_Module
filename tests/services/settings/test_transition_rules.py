"""Settings phase 2: the invariants every stage-transition change keeps (agreed 2026-11-12)."""
import ast
from pathlib import Path

from app.models.pipeline import PipelineStage as S
from app.services.settings.transition_rules import ROUTES_FOR_ROLE, protected_reason, role_allowed, violations

ROUTES = Path(__file__).resolve().parents[3] / "app" / "api" / "routes" / "candidate_actions_routes.py"


def _route_roles() -> dict[str, set[str]]:
    """{path suffix: roles its require_roles accepts} for the candidate-action routes."""
    tree = ast.parse(ROUTES.read_text(encoding="utf-8"))
    found = {}
    for fn in (n for n in tree.body if isinstance(n, ast.FunctionDef)):
        paths = [d.args[0].value for d in fn.decorator_list
                 if isinstance(d, ast.Call) and d.args and isinstance(d.args[0], ast.Constant)]
        roles = {a.attr for n in ast.walk(fn) if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "require_roles"
                 for a in n.args if isinstance(a, ast.Attribute)}
        for path in paths:
            found[path.rsplit("/", 1)[-1]] = roles
    return found


def test_the_role_routes_match_the_backends_role_checks():
    """A role may hold an edge only through a route that really accepts it."""
    routes = _route_roles()
    for role, role_routes in ROUTES_FOR_ROLE.items():
        for route in role_routes:
            assert role in routes[route], (role, route)
    assert "HR_ADMIN" not in routes["stage-move"]          # why HR admins move through bulk-stage-move / reject


def _r(frm, to, roles, prev=None):
    return {"previous_stage": prev, "from_stage": frm, "to_stage": to, "allowed_roles": list(roles)}


# A small table where every stage is reachable.
BASE = [
    _r(S.UPLOADED, S.SCREENING, ["SYSTEM"]), _r(S.SCREENING, S.SHORTLISTED, ["RECRUITER", "SYSTEM"]),
    _r(S.SCREENING, S.HOLD, ["RECRUITER", "SYSTEM"]), _r(S.SCREENING, S.REJECTED, ["RECRUITER", "SYSTEM"]),
    _r(S.SCREENING, S.FRAUD_REVIEW, ["RECRUITER"]), _r(S.SHORTLISTED, S.HM_REVIEW, ["RECRUITER"]),
    _r(S.HM_REVIEW, S.INTERVIEW, ["HIRING_MANAGER"]), _r(S.INTERVIEW, S.SELECTED, ["RECRUITER"]),
    _r(S.REJECTED, S.SCREENING, ["RECRUITER"]), _r(S.SCREENING, S.UPLOADED, ["RECRUITER"]),
]


def test_a_valid_table_breaks_nothing():
    assert violations(BASE) == []


def test_unknown_roles_and_empty_rows_are_refused():
    problems = violations(BASE + [_r(S.SHORTLISTED, S.INTERVIEW, ["RECRUTER"]), _r(S.HOLD, S.SHORTLISTED, [])])
    assert any("RECRUTER isn't a role" in p for p in problems)
    assert any("at least one role" in p for p in problems)


def test_a_role_without_a_route_for_the_move_is_refused():
    assert role_allowed("RECRUITER", S.INTERVIEW) and role_allowed("HR_ADMIN", S.SELECTED)
    assert not role_allowed("SYSTEM", S.SELECTED) and not role_allowed("GUEST", S.REJECTED)


def test_every_stage_must_stay_reachable():
    without_selected = [r for r in BASE if r["to_stage"] != S.SELECTED]
    assert any("Selected could no longer be reached" in p for p in violations(without_selected))


def test_no_new_moves_out_of_a_final_stage_but_existing_ones_stay():
    out_of_selected = _r(S.SELECTED, S.HM_REVIEW, ["RECRUITER"])
    added = {(None, S.SELECTED, S.HM_REVIEW)}
    assert any("final stage" in p for p in violations(BASE + [out_of_selected], added_keys=added))
    assert violations(BASE + [out_of_selected]) == []                # already there: allowed


def test_a_duplicate_move_is_refused():
    assert any("already exists" in p for p in violations(BASE + [_r(S.INTERVIEW, S.SELECTED, ["HR_ADMIN"])]))


def test_the_rows_the_system_relies_on_are_protected():
    assert protected_reason(_r(S.SCREENING, S.HOLD, ["RECRUITER", "SYSTEM"]))          # automated move
    assert protected_reason(_r(S.REJECTED, S.SCREENING, ["RECRUITER"], S.HOLD))        # re-parse
    assert protected_reason(_r(S.HOLD, S.UPLOADED, ["RECRUITER"], S.SCREENING))        # re-upload
    assert protected_reason(_r(S.SHORTLISTED, S.INTERVIEW, ["RECRUITER"])) is None
