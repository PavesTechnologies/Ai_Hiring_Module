"""
Every campaign- or candidate-scoped route runs the shared campaign access check (review items 9-12,
A-H5): a hiring manager or recruiter is refused on someone else's campaign (403, nothing reaches the
service's own work) and let through on their own; HR_ADMIN is let through everywhere.

Real routes over the in-memory access world (tests/support/campaign_access_world.py); the database
is a MagicMock, the SES sender raises, so nothing here can send email.
"""
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.db.session import get_db
from app.enums.constants import API_PREFIX
from tests.support.campaign_access_world import access_world

DENIED = "You do not have access to this campaign."
HM, REC, HR = "HIRING_MANAGER", "RECRUITER", "HR_ADMIN"

# (method, path template, roles the route admits, json body or None)
ITEM_10_ROUTES = [
    ("GET", "/campaign-candidates/campaign/{campaign_id}", (HM, REC, HR), None),
    ("GET", "/campaign-candidates/campaign/{campaign_id}/board", (HM, REC, HR), None),
    ("GET", "/campaign-candidates/campaign/{campaign_id}/summary", (HM, REC, HR), None),
    ("GET", "/campaign-candidates/{campaign_candidate_id}/ai-evaluation", (HM, REC, HR), None),
    ("GET", "/campaign-candidates/{campaign_candidate_id}/composite", (HM, REC, HR), None),
    ("GET", "/campaign-candidates/{campaign_candidate_id}/parse-changes", (HM, REC, HR), None),
    ("GET", "/campaign-candidates/{campaign_candidate_id}/timeline", (HM, REC, HR), None),
    ("GET", "/campaign-candidates/{campaign_candidate_id}/composite-history", (HM, REC, HR), None),
    ("GET", "/campaign-candidates/{campaign_candidate_id}/ranking-details", (HM, REC, HR), None),
    ("POST", "/campaign-candidates/{campaign_candidate_id}/advance-to-interview", (HM, HR), None),
    ("POST", "/campaign-candidates/{campaign_candidate_id}/select", (HM, HR), None),
    ("POST", "/campaign-candidates/{campaign_candidate_id}/reject-interview", (HM, HR),
     {"decision_reason": "Not a fit after the panel"}),
    ("POST", "/campaign-candidates/{campaign_candidate_id}/send-rejection-email", (HM, HR), None),
    ("POST", "/campaign-candidates/{campaign_candidate_id}/send-selection-email", (HM, HR), None),
    ("GET", "/campaigns/{campaign_id}/details", (HM, REC, HR), None),
    ("GET", "/campaigns/{campaign_id}/pipeline-summary", (HM, REC, HR), None),
    ("GET", "/campaigns/{campaign_id}/processing-queue", (HM, REC, HR), None),
    ("GET", "/candidate-actions/campaigns/{campaign_id}/candidates/{campaign_candidate_id}/allowed-transitions",
     (HM, REC, HR), None),
    ("GET", "/candidate-actions/candidates/{campaign_candidate_id}/notes", (HM, REC, HR), None),
    ("POST", "/candidate-actions/note-counts", (HM, REC, HR), {"campaign_candidate_ids": ["{campaign_candidate_id}"]}),
    ("GET", "/dashboard/campaigns/{campaign_id}/skill-suggestions?q=py", (HM, REC, HR), None),
    ("GET", "/dashboard/campaigns/{campaign_id}/skill-filter?skill_ids=00000000-0000-0000-0000-000000000001", (HM, REC, HR), None),
    ("GET", "/dashboard/campaigns/{campaign_id}/candidate-filter", (HM, REC, HR), None),
    ("GET", "/resumes/candidate/{campaign_candidate_id}/parsed-json", (HM, REC, HR), None),
    ("GET", "/monitoring/queue-status?campaign_id={campaign_id}", (HM, REC, HR), None),
]
# Item 9: these five had no role check at all - any intranet token could read a scorecard.
ITEM_9_ROUTES = [
    ("GET", "/campaign-candidates/{campaign_candidate_id}/rejection-history", (HM, REC, HR), None),
    ("GET", "/campaign-candidates/{campaign_candidate_id}/summary", (HM, REC, HR), None),
    ("GET", "/campaign-candidates/{campaign_candidate_id}/deterministic", (HM, REC, HR), None),
    ("GET", "/campaign-candidates/{campaign_candidate_id}/semantic", (HM, REC, HR), None),
    ("GET", "/campaign-candidates/{campaign_candidate_id}", (HM, REC, HR), None),
]
ROUTES = ITEM_10_ROUTES + ITEM_9_ROUTES

OWNER = {HM: "hm-1", REC: "rec-1", HR: "hr-1"}
OUTSIDER = {HM: "hm-2", REC: "rec-2"}


@pytest.fixture
def call():
    from app.main import app
    payload = {}
    with access_world() as world, \
            patch("app.middleware.jwt_middleware.decode_token", lambda token: payload), \
            patch("app.middleware.rbac._ensure_local_user"), \
            patch("app.services.notifications.ses_email_client.SESEmailClient.send_email",
                  side_effect=AssertionError("email sent")):
        app.dependency_overrides[get_db] = lambda: MagicMock()
        http = TestClient(app, base_url=f"http://test{API_PREFIX}", client=("127.0.0.1", 50000),
                          raise_server_exceptions=False)

        def _call(route, user_id, role, side):
            method, template, _, body = route
            ids = {k: str(v) for k, v in world.ids(side).items()}
            payload.clear()
            payload.update({"user_id": user_id, "email": f"{user_id}@example.invalid", "roles": [role]})
            json_body = None
            if body is not None:
                import json
                text = json.dumps(body)
                for key, value in ids.items():
                    text = text.replace("{" + key + "}", value)
                json_body = json.loads(text)
            return http.request(method, template.format(**ids), json=json_body, headers={"Authorization": "Bearer t"})

        yield _call
        app.dependency_overrides.clear()


def _denied(response) -> bool:
    return response.status_code == 403 and DENIED in response.text


def _cases(kind):
    cases = []
    for route in ROUTES:
        for role in route[2]:
            if kind == "outsider" and role == HR:
                continue
            cases.append(pytest.param(route, role, id=f"{route[0]} {route[1]} as {role}"))
    return cases


@pytest.mark.parametrize("route, role", _cases("outsider"))
def test_someone_elses_campaign_is_refused(call, route, role):
    # The user's own id on the other campaign, and an outsider on this one: both refused.
    assert _denied(call(route, OWNER[role], role, "b")), "the owner of campaign A reached campaign B"
    assert _denied(call(route, OUTSIDER[role], role, "a")), "an outsider reached campaign A"


@pytest.mark.parametrize("route, role", _cases("owner"))
def test_their_own_campaign_is_let_through(call, route, role):
    response = call(route, OWNER[role], role, "a")
    assert not _denied(response), response.text


@pytest.mark.parametrize("route", ITEM_9_ROUTES, ids=[r[1] for r in ITEM_9_ROUTES])
def test_a_token_without_a_campaign_role_is_refused_by_role(call, route):
    # An intranet employee whose token carries only an org label ("General").
    response = call(route, "emp-1", "General", "a")
    assert response.status_code == 403 and "Access denied" in response.text
