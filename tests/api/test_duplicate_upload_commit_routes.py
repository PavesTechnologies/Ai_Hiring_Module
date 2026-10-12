"""
Duplicate-file upload fix (separate from scoring v2): HTTP-level tests for the two
routes that create a campaign candidate.

  POST /airs/resumes with resolution="use_existing"   (ResumeIntakeService, real)
  POST /airs/campaign-candidates                      (CampaignCandidateService.add_campaign_candidate, real)

Both must commit the campaign candidate and answer 201 only after the commit
succeeded; a failed commit must never come back as 201. The duplicate warning and
the "use existing" choice are each audited and logged. Nothing here may send email.
"""
import logging
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.dependencies.campaign_candidate import get_campaign_candidate_service
from app.dependencies.resume import get_resume_intake_service
from app.db.session import get_db
from app.enums.constants import API_PREFIX, ActionType
from app.exceptions.resume_exceptions import DuplicateResumeFileException
from app.models.campaigns import CampaignStatus
from app.models.candidates import FileFormat, ParseStatus
from app.models.pipeline import PipelineStage
from app.services.resume.resume_intake_service import ResumeIntakeService

RECRUITER = {"Authorization": "Bearer test"}


def _payload(token):
    return {"user_id": "recruiter-1", "email": "r@example.invalid", "roles": ["RECRUITER"]}


class CommitFailed(RuntimeError):
    pass


@pytest.fixture
def client():
    from app.main import app
    with patch("app.middleware.jwt_middleware.decode_token", _payload), \
            patch("app.middleware.rbac._ensure_local_user"), \
            patch("app.services.notifications.ses_email_client.SESEmailClient.send_email",
                  side_effect=AssertionError("email sent")):
        app.dependency_overrides[get_db] = lambda: MagicMock()
        yield app, TestClient(app, base_url=f"http://test{API_PREFIX}", client=("127.0.0.1", 50000),
                              raise_server_exceptions=False)
    app.dependency_overrides.clear()


def _intake(commit_side_effect=None):
    campaign = SimpleNamespace(id=uuid4(), status=CampaignStatus.ACTIVE, max_candidates=None,
                               prompt_template_id=uuid4(), name="Backend Engineer")
    resume = SimpleNamespace(id=uuid4(), candidate_id=uuid4(), file_format=FileFormat.PDF, task_id=None,
                             parse_status=ParseStatus.PARSED)
    resume_service = MagicMock()
    resume_service.upload.return_value = SimpleNamespace(resume=resume, requires_processing=False)
    cc_service = MagicMock()
    cc_service.create_campaign_candidate.return_value = SimpleNamespace(id=uuid4(), pipeline_stage=PipelineStage.UPLOADED)
    campaign_repo = MagicMock()
    campaign_repo.get_by_id.return_value = campaign
    campaign_repo.get_candidate_count.return_value = 0
    campaign_repo.commit.side_effect = commit_side_effect
    audit = MagicMock()
    service = ResumeIntakeService(resume_service=resume_service, campaign_candidate_service=cc_service,
                                  campaign_repo=campaign_repo, audit_service=audit, task_log_repo=MagicMock())
    return service, campaign, campaign_repo, audit


def _upload(client, campaign):
    return client.post("/resumes", headers=RECRUITER, data={
        "campaign_id": str(campaign.id), "candidate_full_name": "Jane Doe", "candidate_email": "jane@example.invalid",
        "consent_confirmed": "true", "resolution": "use_existing"},
        files={"file": ("resume.pdf", b"%PDF-1.4 test", "application/pdf")})


# --- POST /resumes, "use existing" -------------------------------------------

def test_use_existing_commits_audits_and_logs_then_returns_201(client, caplog):
    app, http = client
    service, campaign, campaign_repo, audit = _intake()
    app.dependency_overrides[get_resume_intake_service] = lambda: service
    with caplog.at_level(logging.INFO):
        response = _upload(http, campaign)
    assert response.status_code == 201
    campaign_repo.commit.assert_called_once()
    assert audit.log.call_args.kwargs["action_type"] == ActionType.RESUME_DUPLICATE_USE_EXISTING
    assert "use existing" in caplog.text


def test_use_existing_never_answers_201_when_the_commit_fails(client):
    app, http = client
    service, campaign, campaign_repo, _ = _intake(commit_side_effect=CommitFailed("db down"))
    app.dependency_overrides[get_resume_intake_service] = lambda: service
    response = _upload(http, campaign)
    assert response.status_code >= 500
    campaign_repo.rollback.assert_called()


# --- POST /campaign-candidates ------------------------------------------------

def _cc_service(commit_side_effect=None):
    from app.services.campaign.campaign_candidate_service import CampaignCandidateService
    service = CampaignCandidateService.__new__(CampaignCandidateService)
    service.campaign_candidate_repo = MagicMock()
    service.campaign_candidate_repo.commit.side_effect = commit_side_effect
    from app.schemas.campaign.campaign_candidate_schema import CampaignCandidateResponse
    created = CampaignCandidateResponse(id=uuid4(), campaign_id=uuid4(), candidate_id=uuid4(), resume_id=uuid4(),
                                        pipeline_stage=PipelineStage.UPLOADED, created_at=datetime.now(timezone.utc))
    service.create_campaign_candidate = MagicMock(return_value=created)
    return service


def _create(http):
    return http.post("/campaign-candidates", headers=RECRUITER, json={
        "campaign_id": str(uuid4()), "candidate_id": str(uuid4()), "resume_id": str(uuid4())})


def test_direct_create_commits_then_returns_201(client):
    app, http = client
    service = _cc_service()
    app.dependency_overrides[get_campaign_candidate_service] = lambda: service
    response = _create(http)
    assert response.status_code == 201
    service.campaign_candidate_repo.commit.assert_called_once()


def test_direct_create_never_answers_201_when_the_commit_fails(client):
    app, http = client
    service = _cc_service(commit_side_effect=CommitFailed("db down"))
    app.dependency_overrides[get_campaign_candidate_service] = lambda: service
    response = _create(http)
    assert response.status_code >= 500
    service.campaign_candidate_repo.rollback.assert_called_once()


# --- the duplicate warning itself ---------------------------------------------

def _upload_service():
    from app.services.resume.resume_upload_service import ResumeUploadService
    service = ResumeUploadService.__new__(ResumeUploadService)
    service.file_validation_service = MagicMock()
    service.resume_repo = MagicMock()
    matched = SimpleNamespace(id=uuid4(), candidate_id=uuid4())
    service.resume_repo.get_by_file_hash_global.return_value = matched
    service.candidate_repo = MagicMock()
    service.audit_service = MagicMock()
    service._build_duplicate_warning = MagicMock(return_value={})
    return service, matched


def _call_upload(service, resolution=None):
    return service.upload(file_bytes=b"x", filename="r.pdf", candidate_full_name="Jane Doe",
                          candidate_email="jane@example.invalid", jurisdiction="GLOBAL", consent_source="UPLOAD",
                          uploaded_by="recruiter-1", actor_role="RECRUITER", source_campaign_id=uuid4(),
                          resolution=resolution)


def test_duplicate_warning_is_audited_logged_and_committed_before_the_409(caplog):
    service, matched = _upload_service()
    with caplog.at_level(logging.INFO), pytest.raises(DuplicateResumeFileException):
        _call_upload(service)
    assert service.audit_service.log.call_args.kwargs["action_type"] == ActionType.RESUME_DUPLICATE_DETECTED
    assert service.audit_service.log.call_args.kwargs["entity_id"] == matched.id
    service.resume_repo.commit.assert_called_once()
    assert "Duplicate resume file detected" in caplog.text


def test_duplicate_warning_still_returned_if_its_audit_fails():
    service, _ = _upload_service()
    service.audit_service.log.side_effect = RuntimeError("audit down")
    with pytest.raises(DuplicateResumeFileException):
        _call_upload(service)
    service.resume_repo.rollback.assert_called_once()


def test_use_existing_choice_is_logged(caplog):
    service, matched = _upload_service()
    with caplog.at_level(logging.INFO):
        result = _call_upload(service, resolution="use_existing")
    assert result.requires_processing is False and result.resume is matched
    assert "'use existing' chosen" in caplog.text



# --- "use existing" candidates are scored like a normal upload ----------------

def test_use_existing_queues_scoring_after_the_commit(client):
    app, http = client
    service, campaign, campaign_repo, _ = _intake()
    order = []
    campaign_repo.commit.side_effect = lambda: order.append("commit")
    app.dependency_overrides[get_resume_intake_service] = lambda: service
    with patch("app.services.resume.resume_intake_service._enqueue_deterministic_scoring",
               side_effect=lambda db, resume_id, log_service: order.append(("score", resume_id))) as queue:
        response = _upload(http, campaign)
    assert response.status_code == 201
    queue.assert_called_once()
    assert order[0] == "commit" and order[1][0] == "score"     # never queued for an uncommitted candidate


def test_use_existing_with_parse_still_running_waits_for_the_parse(client):
    app, http = client
    service, campaign, _, _ = _intake()
    service.resume_service.upload.return_value.resume.parse_status = ParseStatus.PARSING
    app.dependency_overrides[get_resume_intake_service] = lambda: service
    with patch("app.services.resume.resume_intake_service._enqueue_deterministic_scoring") as queue:
        response = _upload(http, campaign)
    assert response.status_code == 201
    queue.assert_not_called()      # the finished parse queues every campaign candidate on the resume


def test_use_existing_queueing_failure_never_fails_the_upload(client):
    app, http = client
    service, campaign, campaign_repo, _ = _intake()
    app.dependency_overrides[get_resume_intake_service] = lambda: service
    with patch("app.services.resume.resume_intake_service._enqueue_deterministic_scoring",
               side_effect=RuntimeError("broker down")):
        response = _upload(http, campaign)
    assert response.status_code == 201
    campaign_repo.commit.assert_called_once()
