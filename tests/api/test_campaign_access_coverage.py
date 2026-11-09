"""
Every campaign- or candidate-scoped route that admits a hiring manager or recruiter runs the shared
campaign access check (review items 9-12, A-H5). A new route added without it fails here.

A route is "scoped" when its path or query names a campaign, campaign candidate, resume, candidate,
bulk job, note, interview, upload decision, dead letter or resume task. It must call CampaignAccess
in its own body, or be listed in CHECKED_ELSEWHERE / NOT_CAMPAIGN_SCOPED with the reason.
"""
import inspect

from fastapi.routing import APIRoute

SCOPED_PARAMS = {"campaign_id", "campaign_candidate_id", "resume_id", "resume_id_1", "resume_id_2", "candidate_id",
                 "bulk_upload_job_id", "note_id", "interview_id", "decision_id", "dlq_id"}

# The check runs inside the service, on the shared rule (app/services/campaign_access.py).
CHECKED_ELSEWHERE = {
    "get_campaign_interviews": "InterviewScheduleService._authorize_campaign_access",
    "get_interview_rounds": "InterviewScheduleService._get_candidate_and_authorize",
    "schedule_interview": "InterviewScheduleService._get_candidate_and_authorize",
    "reschedule_interview": "InterviewScheduleService._get_candidate_and_authorize",
    "cancel_interview": "InterviewScheduleService._get_candidate_and_authorize",
    "request_interview_feedback": "InterviewScheduleService._get_candidate_and_authorize",
    "complete_interview": "InterviewScheduleService._get_candidate_and_authorize",
    "get_interview_feedback": "InterviewFeedbackService.get_feedback_for_round",
    "bulk_send_rejection_email": "per candidate, in send_rejection_email (one outsider never blocks the rest)",
}
# Deliberately not campaign-scoped (product decision recorded in the batch 3 report).
NOT_CAMPAIGN_SCOPED = {
    "search_talent_pool_candidates": "the talent pool exists to find candidates outside your campaigns",
    "get_talent_pool_candidate_profile": "the talent pool exists to find candidates outside your campaigns",
}


def _api_routes():
    """Every APIRoute in the app, through included routers (FastAPI wraps them as _IncludedRouter)."""
    from app.main import app
    found, seen = [], set()

    def walk(routes):
        for route in routes:
            if isinstance(route, APIRoute):
                if id(route) not in seen:
                    seen.add(id(route))
                    found.append(route)
                continue
            inner = getattr(route, "original_router", None) or getattr(route, "router", None)
            if inner is not None and hasattr(inner, "routes"):
                walk(inner.routes)
    walk(app.routes)
    assert len(found) > 200, f"only {len(found)} routes found - the route walk is broken"
    return found


def _admitted_roles(route: APIRoute) -> set[str]:
    roles = set()

    def walk(dependant):
        for dep in dependant.dependencies:
            call = dep.call
            if getattr(call, "__qualname__", "") == "require_roles.<locals>._check":
                closure = dict(zip(call.__code__.co_freevars, (c.cell_contents for c in call.__closure__ or ())))
                roles.update(closure.get("allowed", ()))
            walk(dep)
    walk(route.dependant)
    return roles


def _scoped(route: APIRoute) -> bool:
    names = {p.name for p in route.dependant.path_params} | {p.name for p in route.dependant.query_params}
    if names & SCOPED_PARAMS:
        return True
    return "task_id" in names and "/resumes/" in route.path


def test_every_scoped_route_runs_the_campaign_access_check():
    missing, checked = [], 0
    for route in _api_routes():
        roles = _admitted_roles(route)
        if not roles & {"HIRING_MANAGER", "RECRUITER"} or not _scoped(route):
            continue
        name = route.endpoint.__name__
        if name in CHECKED_ELSEWHERE or name in NOT_CAMPAIGN_SCOPED:
            continue
        checked += 1
        if "CampaignAccess(" not in inspect.getsource(route.endpoint):
            missing.append(f"{sorted(route.methods)} {route.path} ({name})")
    assert checked > 60, f"only {checked} scoped routes checked"
    assert not missing, "scoped routes without the campaign access check:\n" + "\n".join(missing)


def test_the_exception_lists_name_real_routes():
    names = {route.endpoint.__name__ for route in _api_routes()}
    assert set(CHECKED_ELSEWHERE) <= names and set(NOT_CAMPAIGN_SCOPED) <= names
