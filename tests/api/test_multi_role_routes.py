"""
Routes hand the services every role the user holds that the route allows -
here a sign-in holding RECRUITER and HR_ADMIN (with "General" first) - so a
stage transition can be made by whichever of them permits it. And adding a
candidate to a campaign needs HR_ADMIN or RECRUITER at the route (the
assigned-recruiter check is in the service). Services mocked; no email.
"""
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.db.session import get_db
from app.dependencies.candidate_actions import get_bulk_stage_move_service
from app.dependencies.campaign_candidate import get_campaign_candidate_service
from app.enums.constants import API_PREFIX

SIGN_IN = ["General", "RECRUITER", "HR_ADMIN"]
HEADERS = {"Authorization": "Bearer t"}


def _provider(value):
    # a closure, not `lambda value=value: ...` - FastAPI would read that default as a query parameter
    def provide():
        return value
    return provide


@pytest.fixture
def make_client():
    patches = []

    def _make(roles=SIGN_IN, overrides=None):
        from app.main import app
        payload = {"user_id": "u-1", "email": "u@example.invalid", "roles": roles}
        for p in (patch("app.middleware.jwt_middleware.decode_token", lambda token: payload),
                  patch("app.middleware.rbac._ensure_local_user"),
                  patch("app.services.notifications.ses_email_client.SESEmailClient.send_email",
                        side_effect=AssertionError("email sent"))):
            p.start()
            patches.append(p)
        app.dependency_overrides[get_db] = lambda: MagicMock()
        for dependency, value in (overrides or {}).items():
            app.dependency_overrides[dependency] = _provider(value)
        return TestClient(app, base_url=f"http://test{API_PREFIX}", client=("127.0.0.1", 50000),
                          raise_server_exceptions=False)

    yield _make
    from app.main import app
    app.dependency_overrides.clear()
    for p in reversed(patches):
        p.stop()


def _stopping_mock(method):
    service = MagicMock()
    getattr(service, method).side_effect = RuntimeError("stop after the call")      # only the call matters
    return service



@pytest.fixture(autouse=True)
def campaign_access_granted():
    """These tests check which roles reach the services, with made-up ids: the campaign access
    check (tested in tests/services/test_campaign_access.py and the access route tests) is let through."""
    from app.services import campaign_access
    names = [n for n in vars(campaign_access.CampaignAccess) if n.startswith("require_")]
    patchers = [patch.object(campaign_access.CampaignAccess, n, lambda *a, **k: None) for n in names]
    for p in patchers:
        p.start()
    yield
    for p in patchers:
        p.stop()

def test_single_move_sends_only_the_roles_the_route_allows(make_client):
    service = _stopping_mock("move_one")
    http = make_client(overrides={get_bulk_stage_move_service: service})
    http.post(f"/candidate-actions/campaigns/{uuid4()}/candidates/{uuid4()}/stage-move", headers=HEADERS,
              json={"target_stage": "SHORTLISTED"})
    kwargs = service.move_one.call_args.kwargs
    assert kwargs["actor_roles"] == ["RECRUITER"] and kwargs["actor_role"] == "RECRUITER"   # HR_ADMIN isn't allowed here


def test_reject_sends_both_roles(make_client):
    service = _stopping_mock("reject_one")
    http = make_client(overrides={get_bulk_stage_move_service: service})
    http.post(f"/candidate-actions/campaigns/{uuid4()}/candidates/{uuid4()}/reject", headers=HEADERS,
              json={"reason": "Not a fit for this role."})
    assert service.reject_one.call_args.kwargs["actor_roles"] == ["HR_ADMIN", "RECRUITER"]


def test_allowed_transitions_sends_both_roles(make_client):
    service = _stopping_mock("get_allowed_transitions")
    http = make_client(overrides={get_bulk_stage_move_service: service})
    http.get(f"/candidate-actions/campaigns/{uuid4()}/candidates/{uuid4()}/allowed-transitions", headers=HEADERS)
    assert service.get_allowed_transitions.call_args.kwargs["actor_roles"] == ["HR_ADMIN", "RECRUITER"]


def test_reparse_sends_both_roles(make_client):
    http = make_client()
    with patch("app.api.routes.campaign_candidate.ResumeReparseService") as reparse:
        reparse.return_value.request.side_effect = RuntimeError("stop after the call")
        http.post(f"/campaign-candidates/{uuid4()}/reparse", headers=HEADERS, json={"reason": "Missed a section"})
    kwargs = reparse.return_value.request.call_args.kwargs
    assert kwargs["actor_roles"] == ["HR_ADMIN", "RECRUITER"] and kwargs["actor_role"] == "HR_ADMIN"


def _add(http):
    return http.post("/campaign-candidates", headers=HEADERS, json={
        "campaign_id": str(uuid4()), "candidate_id": str(uuid4()), "resume_id": str(uuid4())})


def test_add_candidate_sends_both_roles_for_the_assignment_check(make_client):
    service = _stopping_mock("add_campaign_candidate")
    _add(make_client(overrides={get_campaign_candidate_service: service}))
    assert service.add_campaign_candidate.call_args.kwargs["actor_roles"] == ["HR_ADMIN", "RECRUITER"]


@pytest.mark.parametrize("roles", [["HIRING_MANAGER"], ["General"]])
def test_add_candidate_is_refused_without_hr_admin_or_recruiter(make_client, roles):
    service = MagicMock()
    response = _add(make_client(roles=roles, overrides={get_campaign_candidate_service: service}))
    assert response.status_code == 403
    service.add_campaign_candidate.assert_not_called()
