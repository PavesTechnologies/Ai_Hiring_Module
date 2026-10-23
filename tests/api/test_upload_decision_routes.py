"""
Upload decision routes (list, per bulk job, decide) with the service mocked:
roles, the status filter, and that the service's 403 for someone who is
neither the uploader nor an HR admin reaches the client. No email.
"""
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.db.session import get_db
from app.dependencies.resume import get_upload_decision_service
from app.enums.constants import API_PREFIX
from app.exceptions.resume_exceptions import UploadDecisionForbiddenException
from app.schemas.resume.upload_decision import UploadDecisionDecideResponse, UploadDecisionItem

HEADERS = {"Authorization": "Bearer t"}


@pytest.fixture
def make_client():
    patches = []

    def _make(roles):
        from app.main import app
        payload = {"user_id": "rec-2", "email": "r@example.invalid", "roles": roles}
        for p in (patch("app.middleware.jwt_middleware.decode_token", lambda token: payload),
                  patch("app.middleware.rbac._ensure_local_user"),
                  patch("app.services.notifications.ses_email_client.SESEmailClient.send_email",
                        side_effect=AssertionError("email sent"))):
            p.start()
            patches.append(p)
        app.dependency_overrides[get_db] = lambda: MagicMock()
        service = MagicMock()
        app.dependency_overrides[get_upload_decision_service] = lambda: service
        return service, TestClient(app, base_url=f"http://test{API_PREFIX}", client=("127.0.0.1", 50000),
                                   raise_server_exceptions=False)

    yield _make
    from app.main import app
    app.dependency_overrides.clear()
    for p in reversed(patches):
        p.stop()


def _item(status="PENDING"):
    now = datetime.now(timezone.utc)
    return UploadDecisionItem(
        id=uuid4(), campaign_id=uuid4(), bulk_upload_job_id=uuid4(), bulk_upload_job_file_id=uuid4(),
        campaign_candidate_id=uuid4(), candidate_id=uuid4(), candidate_name="Jane Doe",
        candidate_email_masked="j***@example.com", existing_resume_id=uuid4(), existing_original_filename="old.pdf",
        existing_uploaded_at=now, new_original_filename="new.pdf", uploaded_by="rec-1", created_at=now,
        expires_at=now, status=status,
    )


def test_list_campaign_decisions_defaults_to_pending_and_all_means_every_status(make_client):
    service, http = make_client(["RECRUITER"])
    service.list_for_campaign.return_value = [_item()]
    campaign_id = uuid4()
    response = http.get(f"/campaigns/{campaign_id}/upload-decisions", headers=HEADERS)
    assert response.status_code == 200
    assert response.json()["data"]["total"] == 1
    assert response.json()["data"]["items"][0]["new_original_filename"] == "new.pdf"
    service.list_for_campaign.assert_called_with(campaign_id, "PENDING")
    http.get(f"/campaigns/{campaign_id}/upload-decisions?status=ALL", headers=HEADERS)
    service.list_for_campaign.assert_called_with(campaign_id, None)


def test_list_bulk_job_decisions(make_client):
    service, http = make_client(["HR_ADMIN"])
    service.list_for_job.return_value = [_item("KEPT_EXISTING")]
    job_id = uuid4()
    response = http.get(f"/bulk-uploads/{job_id}/decisions", headers=HEADERS)
    assert response.status_code == 200 and response.json()["data"]["items"][0]["status"] == "KEPT_EXISTING"
    service.list_for_job.assert_called_once_with(job_id)


def test_decide_passes_the_actor_and_returns_the_decision(make_client):
    service, http = make_client(["RECRUITER"])
    new_resume_id = uuid4()
    service.decide.return_value = UploadDecisionDecideResponse(decision=_item("NEW_VERSION_USED"),
                                                               new_resume_id=new_resume_id, task_id=uuid4())
    decision_id = uuid4()
    response = http.post(f"/upload-decisions/{decision_id}/decide", headers=HEADERS,
                         json={"choice": "use_new_version"})
    assert response.status_code == 200
    assert response.json()["data"]["new_resume_id"] == str(new_resume_id)
    assert response.json()["data"]["decision"]["status"] == "NEW_VERSION_USED"
    service.decide.assert_called_once_with(decision_id, "use_new_version", actor_id="rec-2",
                                           actor_roles=["RECRUITER"])


def test_decide_by_another_recruiter_is_403(make_client):
    service, http = make_client(["RECRUITER"])
    service.decide.side_effect = UploadDecisionForbiddenException("Only the uploader or an HR admin can decide.")
    response = http.post(f"/upload-decisions/{uuid4()}/decide", headers=HEADERS, json={"choice": "keep_existing"})
    assert response.status_code == 403 and response.json()["success"] is False


def test_decide_rejects_an_unknown_choice(make_client):
    service, http = make_client(["RECRUITER"])
    response = http.post(f"/upload-decisions/{uuid4()}/decide", headers=HEADERS, json={"choice": "both"})
    assert response.status_code == 422
    service.decide.assert_not_called()


def test_other_roles_cannot_use_the_decision_routes(make_client):
    service, http = make_client(["INTERVIEWER"])
    response = http.get(f"/campaigns/{uuid4()}/upload-decisions", headers=HEADERS)
    assert response.status_code == 403
    service.list_for_campaign.assert_not_called()
