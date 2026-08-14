from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from app.exception_handler.exceptions import NotFoundError
from app.exceptions.storage_exception import StorageException
from app.models.candidates import FileFormat, ParseStatus
from app.models.pipeline import PipelineStage
from app.services.resume.resume_monitoring_service import (
    DEFAULT_RESUME_DOWNLOAD_URL_EXPIRY_SECONDS,
    RESUME_STORAGE_BUCKET,
    ResumeMonitoringService,
)

"""
S02-T01 - View All Resume Versions for a Candidate:
  - get_version_history extended with parse_confidence, uploaded_by, and
    per-version campaign/pipeline-stage usage.
  - get_download_url added: server-generated signed URL for one resume
    version, expiry driven by RESUME_DOWNLOAD_URL_EXPIRY_SECONDS (default 300s).
"""


def _make_resume(
    resume_id=None, candidate_id=None, version_number=1, is_active_version=True,
    parse_confidence_score=0.912, uploaded_by="user-1", bulk_upload_job_id=None,
    file_path="airs/resume-1.pdf",
):
    return SimpleNamespace(
        id=resume_id or uuid4(),
        candidate_id=candidate_id or uuid4(),
        version_number=version_number,
        is_active_version=is_active_version,
        file_format=FileFormat.PDF,
        parse_status=ParseStatus.PARSED,
        parse_confidence_score=parse_confidence_score,
        uploaded_by=uploaded_by,
        bulk_upload_job_id=bulk_upload_job_id,
        created_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
        file_path=file_path,
    )


def _make_service(
    resume_repository=None, campaign_candidate_repository=None,
    user_repository=None, config_repository=None, storage_service=None,
):
    return ResumeMonitoringService(
        resume_repository=resume_repository or MagicMock(),
        candidate_repository=MagicMock(),
        encryption_service=MagicMock(),
        task_log_repository=MagicMock(),
        stage_repository=MagicMock(),
        stage_failure_log_repository=MagicMock(),
        dead_letter_queue_repository=MagicMock(),
        storage_service=storage_service or MagicMock(),
        campaign_candidate_repository=campaign_candidate_repository or MagicMock(),
        user_repository=user_repository or MagicMock(),
        config_repository=config_repository or MagicMock(),
    )


# ----------------------------------------------------------------------
# get_version_history
# ----------------------------------------------------------------------

def test_get_version_history_raises_not_found_when_candidate_has_no_resumes():
    resume_repository = MagicMock()
    resume_repository.get_all_versions_by_candidate.return_value = []
    service = _make_service(resume_repository=resume_repository)

    with pytest.raises(NotFoundError):
        service.get_version_history(uuid4())


def test_get_version_history_includes_parse_confidence_and_uploaded_by_name():
    resume = _make_resume(parse_confidence_score=0.845, uploaded_by="user-1")
    resume_repository = MagicMock()
    resume_repository.get_all_versions_by_candidate.return_value = [resume]

    campaign_candidate_repository = MagicMock()
    campaign_candidate_repository.get_campaign_usage_by_resume_ids.return_value = []

    user_repository = MagicMock()
    user_repository.get_by_ids.return_value = [SimpleNamespace(id="user-1", full_name="Jane Recruiter")]

    service = _make_service(
        resume_repository=resume_repository,
        campaign_candidate_repository=campaign_candidate_repository,
        user_repository=user_repository,
    )

    result = service.get_version_history(resume.candidate_id)

    item = result.versions[0]
    assert item.parse_confidence == 0.845
    assert item.uploaded_by == "Jane Recruiter"


def test_get_version_history_falls_back_to_raw_uploaded_by_id_when_user_not_found():
    resume = _make_resume(uploaded_by="deleted-user")
    resume_repository = MagicMock()
    resume_repository.get_all_versions_by_candidate.return_value = [resume]

    campaign_candidate_repository = MagicMock()
    campaign_candidate_repository.get_campaign_usage_by_resume_ids.return_value = []

    user_repository = MagicMock()
    user_repository.get_by_ids.return_value = []

    service = _make_service(
        resume_repository=resume_repository,
        campaign_candidate_repository=campaign_candidate_repository,
        user_repository=user_repository,
    )

    result = service.get_version_history(resume.candidate_id)

    assert result.versions[0].uploaded_by == "deleted-user"


def test_get_version_history_handles_null_parse_confidence():
    resume = _make_resume(parse_confidence_score=None)
    resume_repository = MagicMock()
    resume_repository.get_all_versions_by_candidate.return_value = [resume]

    campaign_candidate_repository = MagicMock()
    campaign_candidate_repository.get_campaign_usage_by_resume_ids.return_value = []
    service = _make_service(
        resume_repository=resume_repository,
        campaign_candidate_repository=campaign_candidate_repository,
    )

    result = service.get_version_history(resume.candidate_id)

    assert result.versions[0].parse_confidence is None


def test_get_version_history_attaches_campaign_usage_for_each_version():
    resume = _make_resume()
    resume_repository = MagicMock()
    resume_repository.get_all_versions_by_candidate.return_value = [resume]

    campaign_id = uuid4()
    campaign_candidate_repository = MagicMock()
    campaign_candidate_repository.get_campaign_usage_by_resume_ids.return_value = [
        (resume.id, campaign_id, "Backend Engineer", PipelineStage.SCREENING),
    ]

    service = _make_service(
        resume_repository=resume_repository,
        campaign_candidate_repository=campaign_candidate_repository,
    )

    result = service.get_version_history(resume.candidate_id)

    usage = result.versions[0].campaigns
    assert len(usage) == 1
    assert usage[0].campaign_id == campaign_id
    assert usage[0].campaign_name == "Backend Engineer"
    assert usage[0].pipeline_stage == "SCREENING"


def test_get_version_history_gives_empty_campaigns_list_when_version_unused():
    resume = _make_resume()
    resume_repository = MagicMock()
    resume_repository.get_all_versions_by_candidate.return_value = [resume]

    campaign_candidate_repository = MagicMock()
    campaign_candidate_repository.get_campaign_usage_by_resume_ids.return_value = []

    service = _make_service(
        resume_repository=resume_repository,
        campaign_candidate_repository=campaign_candidate_repository,
    )

    result = service.get_version_history(resume.candidate_id)

    assert result.versions[0].campaigns == []


# ----------------------------------------------------------------------
# get_download_url
# ----------------------------------------------------------------------

def test_get_download_url_raises_not_found_for_unknown_resume():
    resume_repository = MagicMock()
    resume_repository.get_by_id.return_value = None
    service = _make_service(resume_repository=resume_repository)

    with pytest.raises(NotFoundError):
        service.get_download_url(uuid4())


def test_get_download_url_uses_default_expiry_when_config_unset():
    resume = _make_resume()
    resume_repository = MagicMock()
    resume_repository.get_by_id.return_value = resume

    config_repository = MagicMock()
    config_repository.get_configs_by_keys.return_value = {}

    storage_service = MagicMock()
    storage_service.generate_signed_url.return_value = "https://signed.example/resume-1.pdf"

    service = _make_service(
        resume_repository=resume_repository, config_repository=config_repository,
        storage_service=storage_service,
    )

    result = service.get_download_url(resume.id)

    assert result.expires_in_seconds == DEFAULT_RESUME_DOWNLOAD_URL_EXPIRY_SECONDS
    assert result.download_url == "https://signed.example/resume-1.pdf"
    assert result.version_number == resume.version_number
    storage_service.generate_signed_url.assert_called_once_with(
        bucket_name=RESUME_STORAGE_BUCKET,
        file_path=resume.file_path,
        expires_in=DEFAULT_RESUME_DOWNLOAD_URL_EXPIRY_SECONDS,
    )


def test_get_download_url_uses_configured_expiry_when_set():
    resume = _make_resume()
    resume_repository = MagicMock()
    resume_repository.get_by_id.return_value = resume

    config_repository = MagicMock()
    config_repository.get_configs_by_keys.return_value = {"RESUME_DOWNLOAD_URL_EXPIRY_SECONDS": "600"}

    storage_service = MagicMock()
    storage_service.generate_signed_url.return_value = "https://signed.example/resume-1.pdf"

    service = _make_service(
        resume_repository=resume_repository, config_repository=config_repository,
        storage_service=storage_service,
    )

    result = service.get_download_url(resume.id)

    assert result.expires_in_seconds == 600
    storage_service.generate_signed_url.assert_called_once_with(
        bucket_name=RESUME_STORAGE_BUCKET,
        file_path=resume.file_path,
        expires_in=600,
    )


def test_get_download_url_propagates_storage_exception():
    """Unlike the parsed-json endpoint's best-effort download_url, this endpoint's whole
    purpose is the URL, so a storage failure should surface (the global StorageException
    handler already turns this into a clean 503), not be silently swallowed."""
    resume = _make_resume()
    resume_repository = MagicMock()
    resume_repository.get_by_id.return_value = resume

    config_repository = MagicMock()
    config_repository.get_configs_by_keys.return_value = {}

    storage_service = MagicMock()
    storage_service.generate_signed_url.side_effect = StorageException("boom")

    service = _make_service(
        resume_repository=resume_repository, config_repository=config_repository,
        storage_service=storage_service,
    )

    with pytest.raises(StorageException):
        service.get_download_url(resume.id)
