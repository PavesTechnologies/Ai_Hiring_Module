"""
Duplicate resume decisions on the single upload (ResumeIntakeService and
POST /resumes): identical file already in the campaign, same candidate with a
different file (409 -> keep_existing / use_new_version), and a known candidate
added to a new campaign. Every dependency mocked - nothing stored, no LLM,
Celery or email.
"""
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.db.session import get_db
from app.dependencies.resume import get_resume_intake_service
from app.enums.constants import API_PREFIX
from app.exceptions.resume_exceptions import (
    CandidateInCampaignNewFileException,
    DuplicateFileInCampaignException,
    ResumeException,
)
from app.models.campaigns import CampaignStatus
from app.models.candidates import FileFormat, ParseStatus
from app.models.pipeline import PipelineStage
from app.schemas.resume.upload_decision import CandidateInCampaignNewFileResponse, DuplicateFileInCampaignResponse
from app.services.resume.resume_intake_service import ResumeIntakeService

MODULE = "app.services.resume.resume_intake_service"


@pytest.fixture(autouse=True)
def _campaign_access_granted():
    """Made-up ids over a mocked database: the campaign access check (tested on its own in
    tests/api/test_campaign_access_routes.py) is let through here."""
    from tests.support.campaign_access_world import campaign_access_granted
    with campaign_access_granted():
        yield


def _cc(campaign_id):
    return SimpleNamespace(id=uuid4(), campaign_id=campaign_id, candidate_id=uuid4(), resume_id=uuid4(),
                           pipeline_stage=PipelineStage.SHORTLISTED)


def _harness(identical=None, existing=None, new_resume_version=1):
    campaign = SimpleNamespace(id=uuid4(), status=CampaignStatus.ACTIVE, prompt_template_id=uuid4(),
                               name="Backend Engineer")
    resume = SimpleNamespace(id=uuid4(), candidate_id=uuid4(), file_format=FileFormat.PDF, task_id=None,
                             version_number=new_resume_version, parse_status=ParseStatus.PENDING)
    resume_service = MagicMock()
    resume_service.upload.return_value = SimpleNamespace(resume=resume, requires_processing=True)
    resume_service.store_file.return_value = ("org_None/resume/new.pdf", SimpleNamespace(file_format=FileFormat.PDF),
                                              "h-new")
    cc_service = MagicMock()
    cc_service.create_campaign_candidate.return_value = SimpleNamespace(
        id=uuid4(), pipeline_stage=PipelineStage.UPLOADED)
    campaign_repo = MagicMock()
    campaign_repo.get_by_id.return_value = campaign
    task_log_repo = MagicMock()
    task_log_repo.create_if_new_idempotency_key.side_effect = lambda log: (log, True)
    decisions = MagicMock()
    decisions.find_identical_file_in_campaign.return_value = identical
    decisions.find_candidate_in_campaign.return_value = existing
    decisions.find_candidate_in_campaign_by_file.return_value = None
    decisions.campaign_candidate_repo.get_by_candidate_id.return_value = []
    service = ResumeIntakeService(resume_service=resume_service, campaign_candidate_service=cc_service,
                                  campaign_repo=campaign_repo, audit_service=MagicMock(),
                                  task_log_repo=task_log_repo, upload_decision_service=decisions)
    return service, campaign, resume, decisions


def _upload(service, campaign, resolution=None):
    return service.upload_resume(
        campaign_id=campaign.id, file_bytes=b"%PDF-1.4 new", filename="new.pdf", candidate_full_name="Jane Doe",
        candidate_email="jane@example.com", jurisdiction="GLOBAL", uploaded_by="rec-1", actor_role="RECRUITER",
        resolution=resolution,
    )


def _nothing_stored(service):
    service.resume_service.upload.assert_not_called()
    service.resume_service.store_file.assert_not_called()
    service.campaign_candidate_service.create_campaign_candidate.assert_not_called()


def test_identical_file_in_campaign_is_skipped_with_409_and_nothing_stored():
    service, campaign, _, decisions = _harness()
    identical = _cc(campaign.id)
    decisions.find_identical_file_in_campaign.return_value = identical
    decisions.duplicate_file_in_campaign_data.return_value = DuplicateFileInCampaignResponse(
        campaign_candidate_id=identical.id, candidate_id=identical.candidate_id, candidate_name="Jane Doe",
        current_pipeline_stage="SHORTLISTED", existing_resume_id=identical.resume_id,
        existing_original_filename="cv.pdf", existing_uploaded_at=datetime.now(timezone.utc),
    )
    # Even with a resolution - an identical file is never asked about.
    with pytest.raises(DuplicateFileInCampaignException) as exc:
        _upload(service, campaign, resolution="use_new_version")
    assert exc.value.status_code == 409
    assert exc.value.data.error_code == "DUPLICATE_FILE_IN_CAMPAIGN"
    assert "Jane Doe" in exc.value.message
    decisions.log_duplicate_skipped.assert_called_once()
    _nothing_stored(service)
    decisions.use_new_version.assert_not_called()


def test_same_candidate_new_file_without_resolution_asks_and_stores_nothing():
    service, campaign, _, decisions = _harness()
    existing = _cc(campaign.id)
    decisions.find_candidate_in_campaign.return_value = existing
    decisions.candidate_in_campaign_data.return_value = "conflict-data"
    for resolution in (None, "use_existing", "upload_anyway"):
        with pytest.raises(CandidateInCampaignNewFileException) as exc:
            _upload(service, campaign, resolution=resolution)
        assert exc.value.status_code == 409 and exc.value.data == "conflict-data"
    decisions.candidate_in_campaign_data.assert_called_with(existing)
    _nothing_stored(service)
    decisions.keep_existing_for_upload.assert_not_called()
    decisions.use_new_version.assert_not_called()


def test_keep_existing_stores_nothing_and_returns_the_existing_resume():
    service, campaign, _, decisions = _harness()
    existing = _cc(campaign.id)
    decisions.find_candidate_in_campaign.return_value = existing
    existing_resume = SimpleNamespace(id=existing.resume_id, task_id=None, parse_status=ParseStatus.PARSED)
    decisions.resume_repo.get_by_id.return_value = existing_resume

    result = _upload(service, campaign, resolution="keep_existing")

    assert result.outcome == "KEPT_EXISTING" and result.requires_processing is False
    assert result.resume is existing_resume and result.campaign_candidate is existing and result.task_id is None
    decisions.keep_existing_for_upload.assert_called_once_with(existing, filename="new.pdf", actor_id="rec-1",
                                                               actor_role="RECRUITER")
    _nothing_stored(service)


def test_use_new_version_stores_the_file_and_uses_the_shared_service():
    service, campaign, _, decisions = _harness()
    existing = _cc(campaign.id)
    decisions.find_candidate_in_campaign.return_value = existing
    new_resume = SimpleNamespace(id=uuid4(), version_number=3)
    previous = SimpleNamespace(id=existing.resume_id)
    task_id = uuid4()
    decisions.use_new_version.return_value = (new_resume, previous, task_id)

    result = _upload(service, campaign, resolution="use_new_version")

    service.resume_service.store_file.assert_called_once()
    kwargs = decisions.use_new_version.call_args.kwargs
    assert decisions.use_new_version.call_args.args == (existing, campaign)
    assert kwargs["storage_path"] == "org_None/resume/new.pdf" and kwargs["file_hash"] == "h-new"
    assert kwargs["original_filename"] == "new.pdf" and kwargs["actor_id"] == "rec-1"
    assert result.outcome == "NEW_VERSION_USED" and result.resume is new_resume
    assert result.previous_resume_id == existing.resume_id and result.task_id == task_id
    # Never through the normal upload / a new campaign candidate.
    service.resume_service.upload.assert_not_called()
    service.campaign_candidate_service.create_campaign_candidate.assert_not_called()


def test_use_new_version_failure_deletes_the_stored_file():
    service, campaign, _, decisions = _harness()
    decisions.find_candidate_in_campaign.return_value = _cc(campaign.id)
    decisions.use_new_version.side_effect = RuntimeError("db down")
    with pytest.raises(RuntimeError):
        _upload(service, campaign, resolution="use_new_version")
    service.resume_service.storage_service.delete_file.assert_called_once_with("airs_resumes",
                                                                              "org_None/resume/new.pdf")


def test_new_file_resolution_for_a_candidate_not_in_the_campaign_is_rejected():
    service, campaign, _, _ = _harness()
    with pytest.raises(ResumeException) as exc:
        _upload(service, campaign, resolution="keep_existing")
    assert exc.value.status_code == 400
    _nothing_stored(service)


def test_known_candidate_new_file_in_a_new_campaign_marks_other_campaigns_out_of_date():
    service, campaign, resume, decisions = _harness(new_resume_version=2)
    other = SimpleNamespace(id=uuid4(), campaign_id=uuid4(), score_out_of_date_at=None, score_out_of_date_reason=None)
    decisions.campaign_candidate_repo.get_by_candidate_id.return_value = [other]
    with patch(f"{MODULE}.process_resume_document"), patch(f"{MODULE}.CandidateChangeNotifier"):
        result = _upload(service, campaign)
    assert result.outcome == "UPLOADED"
    decisions.campaign_candidate_repo.get_by_candidate_id.assert_called_once_with(resume.candidate_id)
    assert other.score_out_of_date_at is not None and "Backend Engineer" in other.score_out_of_date_reason
    service.campaign_candidate_service.create_campaign_candidate.assert_called_once()


def test_first_resume_of_a_new_candidate_stamps_nothing():
    service, campaign, _, decisions = _harness(new_resume_version=1)
    with patch(f"{MODULE}.process_resume_document"), patch(f"{MODULE}.CandidateChangeNotifier"):
        _upload(service, campaign)
    decisions.campaign_candidate_repo.get_by_candidate_id.assert_not_called()


# --- what a client receives -----------------------------------------------------

@pytest.fixture
def client():
    from app.main import app
    payload = {"user_id": "rec-1", "email": "r@example.invalid", "roles": ["RECRUITER"]}
    with patch("app.middleware.jwt_middleware.decode_token", lambda token: payload), \
            patch("app.middleware.rbac._ensure_local_user"), \
            patch("app.services.notifications.ses_email_client.SESEmailClient.send_email",
                  side_effect=AssertionError("email sent")):
        app.dependency_overrides[get_db] = lambda: MagicMock()
        yield app, TestClient(app, base_url=f"http://test{API_PREFIX}", client=("127.0.0.1", 50000),
                              raise_server_exceptions=False)
    app.dependency_overrides.clear()


def _post(http, campaign, resolution=None):
    data = {"campaign_id": str(campaign.id), "candidate_full_name": "Jane Doe", "candidate_email": "jane@example.com",
            "consent_confirmed": "true"}
    if resolution:
        data["resolution"] = resolution
    return http.post("/resumes", headers={"Authorization": "Bearer t"}, data=data,
                     files={"file": ("new.pdf", b"%PDF-1.4 new", "application/pdf")})


def test_route_409_candidate_in_campaign_new_file_body(client):
    app, http = client
    service, campaign, _, decisions = _harness()
    existing = _cc(campaign.id)
    decisions.find_candidate_in_campaign.return_value = existing
    decisions.candidate_in_campaign_data.return_value = CandidateInCampaignNewFileResponse(
        campaign_candidate_id=existing.id, candidate_id=existing.candidate_id, candidate_name="Jane Doe",
        current_pipeline_stage="SHORTLISTED", existing_resume_id=existing.resume_id,
        existing_original_filename="cv.pdf", existing_uploaded_at=datetime(2026, 10, 1, tzinfo=timezone.utc),
        existing_version_number=1, available_resolutions=["keep_existing", "use_new_version"],
    )
    app.dependency_overrides[get_resume_intake_service] = lambda: service
    response = _post(http, campaign)
    assert response.status_code == 409
    body = response.json()
    assert body["success"] is False
    assert body["data"]["error_code"] == "CANDIDATE_IN_CAMPAIGN_NEW_FILE"
    assert body["data"]["campaign_candidate_id"] == str(existing.id)
    assert body["data"]["existing_original_filename"] == "cv.pdf"
    assert body["data"]["available_resolutions"] == ["keep_existing", "use_new_version"]


def test_route_keep_existing_and_use_new_version_success_bodies(client):
    app, http = client
    service, campaign, _, decisions = _harness()
    existing = _cc(campaign.id)
    decisions.find_candidate_in_campaign.return_value = existing
    decisions.resume_repo.get_by_id.return_value = SimpleNamespace(
        id=existing.resume_id, task_id=None, parse_status=ParseStatus.PARSED, version_number=1)
    app.dependency_overrides[get_resume_intake_service] = lambda: service

    kept = _post(http, campaign, "keep_existing")
    assert kept.status_code == 201
    assert kept.json()["data"]["outcome"] == "KEPT_EXISTING"
    assert kept.json()["data"]["resume_id"] == str(existing.resume_id)
    assert kept.json()["data"]["pipeline_stage"] == "SHORTLISTED"
    assert "existing resume was kept" in kept.json()["message"]

    new_resume = SimpleNamespace(id=uuid4(), version_number=2, parse_status=ParseStatus.PENDING)
    decisions.use_new_version.return_value = (new_resume, SimpleNamespace(id=existing.resume_id), uuid4())
    used = _post(http, campaign, "use_new_version")
    assert used.status_code == 201
    data = used.json()["data"]
    assert data["outcome"] == "NEW_VERSION_USED" and data["resume_id"] == str(new_resume.id)
    assert data["previous_resume_id"] == str(existing.resume_id) and data["version_number"] == 2
    assert data["pipeline_stage"] == "SHORTLISTED" and data["parse_status"] == "PENDING"


def test_route_409_duplicate_file_in_campaign_body(client):
    app, http = client
    service, campaign, _, decisions = _harness()
    identical = _cc(campaign.id)
    decisions.find_identical_file_in_campaign.return_value = identical
    decisions.duplicate_file_in_campaign_data.return_value = DuplicateFileInCampaignResponse(
        campaign_candidate_id=identical.id, candidate_id=identical.candidate_id, candidate_name="Jane Doe",
        current_pipeline_stage="SHORTLISTED", existing_resume_id=identical.resume_id,
        existing_original_filename="cv.pdf", existing_uploaded_at=datetime(2026, 10, 1, tzinfo=timezone.utc),
    )
    app.dependency_overrides[get_resume_intake_service] = lambda: service
    response = _post(http, campaign)
    assert response.status_code == 409
    body = response.json()
    assert body["success"] is False and "Jane Doe" in body["message"]
    assert body["data"]["error_code"] == "DUPLICATE_FILE_IN_CAMPAIGN"
    assert body["data"]["existing_resume_id"] == str(identical.resume_id)
    _nothing_stored(service)


# --- the file belongs to a candidate already in this campaign, under another email ---------------

def _owner_harness():
    service, campaign, resume, decisions = _harness()
    owner = _cc(campaign.id)                                     # in the campaign; the typed email isn't theirs
    decisions.find_candidate_in_campaign_by_file.return_value = owner
    decisions.candidate_in_campaign_data.return_value = CandidateInCampaignNewFileResponse(
        campaign_candidate_id=owner.id, candidate_id=owner.candidate_id, candidate_name="Jane Doe",
        current_pipeline_stage="SHORTLISTED", existing_resume_id=owner.resume_id,
        existing_original_filename="cv_v2.pdf", existing_uploaded_at=datetime.now(timezone.utc),
        existing_version_number=2, available_resolutions=["keep_existing", "use_new_version"],
    )
    return service, campaign, owner, decisions


@pytest.mark.parametrize("resolution", [None, "upload_anyway", "use_existing"])
def test_a_file_owned_by_a_candidate_in_this_campaign_goes_to_the_decision_not_upload_anyway(resolution):
    service, campaign, owner, decisions = _owner_harness()
    with pytest.raises(CandidateInCampaignNewFileException) as exc:
        _upload(service, campaign, resolution=resolution)
    assert exc.value.status_code == 409
    decisions.candidate_in_campaign_data.assert_called_once_with(owner)
    _nothing_stored(service)                                    # no orphaned version


def test_the_owner_case_resolves_like_any_candidate_in_the_campaign():
    service, campaign, owner, decisions = _owner_harness()
    with patch.object(service, "_resolve_candidate_in_campaign", return_value="resolved") as resolve:
        assert _upload(service, campaign, resolution="keep_existing") == "resolved"
    assert resolve.call_args.args[0] is owner and resolve.call_args.kwargs["resolution"] == "keep_existing"
    decisions.find_candidate_in_campaign_by_file.assert_called_once()


def test_an_email_match_wins_over_the_file_owner_lookup():
    service, campaign, _, decisions = _harness(existing=_cc(uuid4()))
    with pytest.raises(CandidateInCampaignNewFileException):
        _upload(service, campaign)
    decisions.find_candidate_in_campaign_by_file.assert_not_called()


def test_a_file_owned_only_outside_this_campaign_keeps_todays_flow():
    service, campaign, _, decisions = _harness()               # owner lookup: None
    _upload(service, campaign, resolution="upload_anyway")
    service.resume_service.upload.assert_called_once()

