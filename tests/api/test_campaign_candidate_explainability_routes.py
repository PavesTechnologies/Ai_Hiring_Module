"""
M10-E03 Phase 2: structural RBAC verification for the three new
explainability routes (timeline, composite-history, ranking-details).
Mirrors test_campaign_candidate_ranking_routes.py's exact approach -
inspecting the actual FastAPI route/dependency graph built at import time,
since this project has no TestClient/HTTP-level test infrastructure.
"""
from app.api.routes.campaign_candidate import router
from app.models.identity import UserRole

_TIMELINE_PATH = "/campaign-candidates/{campaign_candidate_id}/timeline"
_COMPOSITE_HISTORY_PATH = "/campaign-candidates/{campaign_candidate_id}/composite-history"
_RANKING_DETAILS_PATH = "/campaign-candidates/{campaign_candidate_id}/ranking-details"

_EXPECTED_ROLES = frozenset({
    UserRole.HR_ADMIN.value, UserRole.RECRUITER.value, UserRole.HIRING_MANAGER.value,
})


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


def test_all_three_explainability_routes_are_registered_at_the_documented_paths():
    for path in (_TIMELINE_PATH, _COMPOSITE_HISTORY_PATH, _RANKING_DETAILS_PATH):
        assert _get_route(path).path == path


def test_timeline_route_allows_hr_admin_recruiter_hiring_manager():
    assert _allowed_roles(_get_route(_TIMELINE_PATH)) == _EXPECTED_ROLES


def test_composite_history_route_allows_hr_admin_recruiter_hiring_manager():
    assert _allowed_roles(_get_route(_COMPOSITE_HISTORY_PATH)) == _EXPECTED_ROLES


def test_ranking_details_route_allows_hr_admin_recruiter_hiring_manager():
    assert _allowed_roles(_get_route(_RANKING_DETAILS_PATH)) == _EXPECTED_ROLES


def test_response_models_are_the_new_dedicated_schemas_not_duplicates_of_existing_ones():
    from app.schemas.campaign.campaign_candidate_schema import (
        CandidateCompositeScoreHistoryResponse,
        CandidateRankingDetailsResponse,
        CandidateTimelineResponse,
    )

    assert CandidateTimelineResponse.__name__ in str(_get_route(_TIMELINE_PATH).response_model)
    assert CandidateCompositeScoreHistoryResponse.__name__ in str(_get_route(_COMPOSITE_HISTORY_PATH).response_model)
    assert CandidateRankingDetailsResponse.__name__ in str(_get_route(_RANKING_DETAILS_PATH).response_model)


def test_existing_scorecard_route_still_registered_unchanged():
    """Backward compatibility: the pre-existing scorecard route must still exist at its own path."""
    route = _get_route("/campaign-candidates/{campaign_candidate_id}")
    assert route.path == "/campaign-candidates/{campaign_candidate_id}"
