"""
POST /airs/resumes records the role that authorised the upload - HR_ADMIN, or
RECRUITER - never the first entry on the sign-in (some tokens list "General"
first). The intake service is mocked; nothing is stored. No email.
"""
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.db.session import get_db
from app.dependencies.resume import get_resume_intake_service
from app.enums.constants import API_PREFIX


@pytest.mark.parametrize("roles, expected", [
    (["General", "RECRUITER"], "RECRUITER"),
    (["General", "RECRUITER", "HR_ADMIN"], "HR_ADMIN"),
    (["HR_ADMIN"], "HR_ADMIN"),
])
def test_upload_records_the_authorising_role(roles, expected):
    from app.main import app
    payload = {"user_id": "rec-1", "email": "r@example.invalid", "roles": roles}
    service = MagicMock()
    service.upload_resume.side_effect = RuntimeError("stop after the call")   # only the call matters here
    with patch("app.middleware.jwt_middleware.decode_token", lambda token: payload), \
            patch("app.middleware.rbac._ensure_local_user"), \
            patch("app.services.notifications.ses_email_client.SESEmailClient.send_email",
                  side_effect=AssertionError("email sent")):
        app.dependency_overrides[get_db] = lambda: MagicMock()
        app.dependency_overrides[get_resume_intake_service] = lambda: service
        try:
            http = TestClient(app, base_url=f"http://test{API_PREFIX}", client=("127.0.0.1", 50000),
                              raise_server_exceptions=False)
            http.post("/resumes", headers={"Authorization": "Bearer t"}, data={
                "campaign_id": str(uuid4()), "candidate_full_name": "Jane Doe",
                "candidate_email": "jane@example.invalid", "consent_confirmed": "true"},
                files={"file": ("resume.pdf", b"%PDF-1.4 test", "application/pdf")})
        finally:
            app.dependency_overrides.clear()
    service.upload_resume.assert_called_once()
    assert service.upload_resume.call_args.kwargs["actor_role"] == expected


@pytest.mark.parametrize("roles, expected", [
    (["General", "RECRUITER"], ["RECRUITER"]),        # used to send "General" - refused by every transition
    (["HR_ADMIN", "RECRUITER"], ["HR_ADMIN", "RECRUITER"]),   # the service moves on whichever the transition permits
    (["General", "HR_ADMIN"], ["HR_ADMIN"]),
])
def test_resubmission_sends_the_role_that_authorises_the_transition(roles, expected):
    from app.dependencies.campaign_candidate import get_campaign_candidate_service
    from app.main import app
    payload = {"user_id": "rec-1", "email": "r@example.invalid", "roles": roles}
    service = MagicMock()
    service.update_resume_for_resubmission.side_effect = RuntimeError("stop after the call")
    with patch("app.middleware.jwt_middleware.decode_token", lambda token: payload), \
            patch("app.middleware.rbac._ensure_local_user"), \
            patch("app.services.notifications.ses_email_client.SESEmailClient.send_email",
                  side_effect=AssertionError("email sent")):
        app.dependency_overrides[get_db] = lambda: MagicMock()
        app.dependency_overrides[get_campaign_candidate_service] = lambda: service
        try:
            http = TestClient(app, base_url=f"http://test{API_PREFIX}", client=("127.0.0.1", 50000),
                              raise_server_exceptions=False)
            http.post(f"/campaign-candidates/{uuid4()}/update-resume", headers={"Authorization": "Bearer t"},
                      files={"file": ("resume.pdf", b"%PDF-1.4 test", "application/pdf")})
        finally:
            app.dependency_overrides.clear()
    service.update_resume_for_resubmission.assert_called_once()
    kwargs = service.update_resume_for_resubmission.call_args.kwargs
    assert kwargs["actor_roles"] == expected and kwargs["actor_role"] == expected[0]
