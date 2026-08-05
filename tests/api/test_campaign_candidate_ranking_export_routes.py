"""
M10-E03 Phase 3: structural RBAC verification for the new Campaign Ranked
Candidate Export route. Mirrors test_campaign_candidate_ranking_routes.py/
test_campaign_candidate_explainability_routes.py's exact approach -
inspecting the actual FastAPI route/dependency graph built at import time,
since this project has no TestClient/HTTP-level test infrastructure.
"""
from app.api.routes.campaign_candidate import router
from app.models.identity import UserRole

_EXPORT_PATH = "/campaign-candidates/campaign/{campaign_id}/export"
_RANKED_LIST_PATH = "/campaign-candidates/campaign/{campaign_id}"
_EXPORT_REJECTED_PATH = "/campaign-candidates/campaign/{campaign_id}/export-rejected"


def _get_route(path: str):
    for route in router.routes:
        if route.path == path and "GET" in route.methods:
            return route
    raise AssertionError(f"No GET route registered for {path}")


def _allowed_roles(route) -> frozenset:
    for dependency in route.dependant.dependencies:
        call = dependency.call
        if call.__name__ == "_check" and "allowed" in call.__code__.co_freevars:
            index = call.__code__.co_freevars.index("allowed")
            return call.__closure__[index].cell_contents
    raise AssertionError(f"No require_roles(...) dependency found on {route.path}")


def test_export_route_is_registered_at_the_documented_path():
    assert _get_route(_EXPORT_PATH).path == _EXPORT_PATH


def test_export_route_is_hr_admin_only_not_the_read_only_convention():
    """
    Exports follow the export RBAC convention (HR_ADMIN only), NOT the
    read-only ranked-list/explainability convention (HR_ADMIN + RECRUITER
    + HIRING_MANAGER) - deliberately stricter, matching every other export
    endpoint in this codebase (export-rejected, override-report/export,
    rejection-analytics/export).
    """
    assert _allowed_roles(_get_route(_EXPORT_PATH)) == frozenset({UserRole.HR_ADMIN.value})


def test_export_route_matches_existing_export_rejected_rbac_exactly():
    assert _allowed_roles(_get_route(_EXPORT_PATH)) == _allowed_roles(_get_route(_EXPORT_REJECTED_PATH))


def test_ranked_list_route_rbac_unaffected_by_the_new_export_route():
    """Backward compatibility: Phase 1's ranked-list RBAC (3 roles) must remain unchanged."""
    assert _allowed_roles(_get_route(_RANKED_LIST_PATH)) == frozenset({
        UserRole.HR_ADMIN.value, UserRole.RECRUITER.value, UserRole.HIRING_MANAGER.value,
    })


def test_export_route_has_no_response_model_matching_other_streaming_exports():
    """StreamingResponse exports never declare a Pydantic response_model - matches export-rejected/override-report/export."""
    assert _get_route(_EXPORT_PATH).response_model is None
    assert _get_route(_EXPORT_REJECTED_PATH).response_model is None
