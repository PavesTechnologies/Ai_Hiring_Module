from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from app.exception_handler.exceptions import NotFoundError, UnprocessableError
from app.exceptions.campaign_exceptions import CampaignException
from app.models.async_tasks import TaskStatus
from app.models.campaigns import CampaignStatus
from app.models.candidates import ParseStatus
from app.models.pipeline import PipelineStage
from app.services.campaign.resume_selection_service import (
    EvaluatedResume,
    ResumeSelectionResult,
    SelectionMethod,
)
from app.services.resume.resume_service import ResumeService
from app.services.talent_pool.talent_pool_service import TalentPoolService

"""
M13-E01 S01 T03 - Add Candidate Directly to New Campaign.

Resume selection itself (eligibility filtering, DIRECT vs COMPARED,
scoring) is ResumeSelectionService's responsibility and is covered by
tests/services/campaign/test_resume_selection_service.py. These tests only
verify TalentPoolService's orchestration: it delegates to
ResumeSelectionService, uses the returned selected resume to create the
campaign_candidate, and records the selection metadata on the audit entry.
"""


def _make_candidate():
    return SimpleNamespace(id=uuid4())


def _make_campaign(status=CampaignStatus.ACTIVE, prompt_template_id=None):
    return SimpleNamespace(id=uuid4(), status=status, prompt_template_id=prompt_template_id or uuid4())


def _make_resume(parse_status=ParseStatus.PARSED, parser_version=ResumeService.PARSER_VERSION):
    return SimpleNamespace(
        id=uuid4(),
        parse_status=parse_status,
        parser_version=parser_version,
        created_at=datetime.now(timezone.utc),
    )


def _make_campaign_candidate(campaign_id, candidate_id, resume_id):
    return SimpleNamespace(
        id=uuid4(),
        campaign_id=campaign_id,
        candidate_id=candidate_id,
        resume_id=resume_id,
        pipeline_stage=PipelineStage.UPLOADED,
    )


def _direct_selection_result(resume):
    return ResumeSelectionResult(
        selected_resume=resume,
        selection_method=SelectionMethod.DIRECT,
        evaluated_resumes=[EvaluatedResume(
            resume=resume, deterministic_score=None, deterministic_passed=None,
            semantic_score=None, semantic_passed=None, selection_score=None, is_selected=True,
        )],
    )


def make_service(
    candidate_repo=None,
    resume_repo=None,
    campaign_repo=None,
    campaign_candidate_repo=None,
    audit_service=None,
    celery_task_log_service=None,
    resume_selection_service=None,
):
    return TalentPoolService(
        candidate_repo=candidate_repo or MagicMock(),
        resume_repo=resume_repo or MagicMock(),
        campaign_repo=campaign_repo or MagicMock(),
        campaign_candidate_repo=campaign_candidate_repo or MagicMock(),
        consent_repo=MagicMock(),
        encryption_service=MagicMock(),
        audit_service=audit_service or MagicMock(),
        celery_task_log_service=celery_task_log_service or MagicMock(),
        resume_selection_service=resume_selection_service or MagicMock(),
    )


def test_add_candidate_to_campaign_raises_not_found_when_candidate_missing():
    candidate_repo = MagicMock()
    candidate_repo.get_by_id.return_value = None
    service = make_service(candidate_repo=candidate_repo)

    with pytest.raises(NotFoundError):
        service.add_candidate_to_campaign(uuid4(), uuid4(), actor_id="user-1")


def test_add_candidate_to_campaign_raises_404_when_campaign_missing():
    candidate_repo = MagicMock()
    candidate_repo.get_by_id.return_value = _make_candidate()
    campaign_repo = MagicMock()
    campaign_repo.get_by_id_for_update.return_value = None
    service = make_service(candidate_repo=candidate_repo, campaign_repo=campaign_repo)

    with pytest.raises(CampaignException) as exc_info:
        service.add_candidate_to_campaign(uuid4(), uuid4(), actor_id="user-1")
    assert exc_info.value.status_code == 404


def test_add_candidate_to_campaign_raises_409_when_campaign_paused():
    candidate_repo = MagicMock()
    candidate_repo.get_by_id.return_value = _make_candidate()
    campaign_repo = MagicMock()
    campaign_repo.get_by_id_for_update.return_value = _make_campaign(status=CampaignStatus.PAUSED)
    service = make_service(candidate_repo=candidate_repo, campaign_repo=campaign_repo)

    with pytest.raises(CampaignException) as exc_info:
        service.add_candidate_to_campaign(uuid4(), uuid4(), actor_id="user-1")
    assert exc_info.value.status_code == 409


def test_add_candidate_to_campaign_raises_403_when_campaign_closed():
    candidate_repo = MagicMock()
    candidate_repo.get_by_id.return_value = _make_candidate()
    campaign_repo = MagicMock()
    campaign_repo.get_by_id_for_update.return_value = _make_campaign(status=CampaignStatus.CLOSED)
    service = make_service(candidate_repo=candidate_repo, campaign_repo=campaign_repo)

    with pytest.raises(CampaignException) as exc_info:
        service.add_candidate_to_campaign(uuid4(), uuid4(), actor_id="user-1")
    assert exc_info.value.status_code == 403


def test_add_candidate_to_campaign_raises_409_when_already_added():
    candidate_repo = MagicMock()
    candidate_repo.get_by_id.return_value = _make_candidate()
    campaign_repo = MagicMock()
    campaign_repo.get_by_id_for_update.return_value = _make_campaign()
    campaign_candidate_repo = MagicMock()
    campaign_candidate_repo.get_by_campaign_and_candidate.return_value = SimpleNamespace(id=uuid4())
    service = make_service(
        candidate_repo=candidate_repo, campaign_repo=campaign_repo, campaign_candidate_repo=campaign_candidate_repo,
    )

    with pytest.raises(CampaignException) as exc_info:
        service.add_candidate_to_campaign(uuid4(), uuid4(), actor_id="user-1")
    assert exc_info.value.status_code == 409


def test_add_candidate_to_campaign_propagates_unprocessable_from_resume_selection():
    """
    No eligible resume is entirely ResumeSelectionService's determination
    (see its own tests) - TalentPoolService must simply let that exception
    propagate, not swallow or reinterpret it.
    """
    candidate_repo = MagicMock()
    candidate_repo.get_by_id.return_value = _make_candidate()
    campaign_repo = MagicMock()
    campaign_repo.get_by_id_for_update.return_value = _make_campaign()
    campaign_candidate_repo = MagicMock()
    campaign_candidate_repo.get_by_campaign_and_candidate.return_value = None
    resume_selection_service = MagicMock()
    resume_selection_service.select_resume_for_campaign.side_effect = UnprocessableError(
        "Candidate has no eligible resume for campaign assignment.",
    )
    service = make_service(
        candidate_repo=candidate_repo,
        campaign_repo=campaign_repo,
        campaign_candidate_repo=campaign_candidate_repo,
        resume_selection_service=resume_selection_service,
    )

    with pytest.raises(UnprocessableError):
        service.add_candidate_to_campaign(uuid4(), uuid4(), actor_id="user-1")


def test_add_candidate_to_campaign_delegates_to_resume_selection_service_with_candidate_id_and_campaign():
    candidate = _make_candidate()
    campaign = _make_campaign()
    resume = _make_resume()

    candidate_repo = MagicMock()
    candidate_repo.get_by_id.return_value = candidate
    campaign_repo = MagicMock()
    campaign_repo.get_by_id_for_update.return_value = campaign
    campaign_candidate_repo = MagicMock()
    campaign_candidate_repo.get_by_campaign_and_candidate.return_value = None
    campaign_candidate_repo.create_idempotent.return_value = (
        _make_campaign_candidate(campaign.id, candidate.id, resume.id), True,
    )
    resume_selection_service = MagicMock()
    resume_selection_service.select_resume_for_campaign.return_value = _direct_selection_result(resume)

    resume_repo = MagicMock()
    resume_repo.db = MagicMock()

    service = make_service(
        candidate_repo=candidate_repo,
        campaign_repo=campaign_repo,
        campaign_candidate_repo=campaign_candidate_repo,
        resume_repo=resume_repo,
        resume_selection_service=resume_selection_service,
    )

    with patch("app.services.talent_pool.talent_pool_service._enqueue_resume_embedding"):
        service.add_candidate_to_campaign(candidate.id, campaign.id, actor_id="user-1")

    resume_selection_service.select_resume_for_campaign.assert_called_once_with(candidate.id, campaign)


def _setup_happy_path(parser_version=ResumeService.PARSER_VERSION, selection_method=SelectionMethod.DIRECT):
    candidate = _make_candidate()
    campaign = _make_campaign()
    resume = _make_resume(parser_version=parser_version)

    candidate_repo = MagicMock()
    candidate_repo.get_by_id.return_value = candidate
    campaign_repo = MagicMock()
    campaign_repo.get_by_id_for_update.return_value = campaign
    campaign_candidate_repo = MagicMock()
    campaign_candidate_repo.get_by_campaign_and_candidate.return_value = None
    campaign_candidate = _make_campaign_candidate(campaign.id, candidate.id, resume.id)
    campaign_candidate_repo.create_idempotent.return_value = (campaign_candidate, True)

    resume_repo = MagicMock()
    resume_repo.db = MagicMock()

    resume_selection_service = MagicMock()
    if selection_method == SelectionMethod.DIRECT:
        resume_selection_service.select_resume_for_campaign.return_value = _direct_selection_result(resume)
    else:
        resume_selection_service.select_resume_for_campaign.return_value = ResumeSelectionResult(
            selected_resume=resume,
            selection_method=SelectionMethod.COMPARED,
            evaluated_resumes=[
                EvaluatedResume(
                    resume=resume, deterministic_score=85.0, deterministic_passed=True,
                    semantic_score=0.8, semantic_passed=True, selection_score=82.0, is_selected=True,
                ),
                EvaluatedResume(
                    resume=_make_resume(), deterministic_score=40.0, deterministic_passed=False,
                    semantic_score=0.5, semantic_passed=False, selection_score=45.0, is_selected=False,
                ),
            ],
        )

    audit_service = MagicMock()
    celery_task_log_service = MagicMock()
    celery_task_log_service.repository = MagicMock()
    celery_task_log_service.repository.create_if_new_idempotency_key.return_value = (
        SimpleNamespace(task_id=str(uuid4()), status=TaskStatus.QUEUED), True,
    )

    service = make_service(
        candidate_repo=candidate_repo,
        campaign_repo=campaign_repo,
        campaign_candidate_repo=campaign_candidate_repo,
        resume_repo=resume_repo,
        audit_service=audit_service,
        celery_task_log_service=celery_task_log_service,
        resume_selection_service=resume_selection_service,
    )
    return service, candidate, campaign, resume, campaign_candidate_repo, audit_service, celery_task_log_service


def test_add_candidate_to_campaign_uses_selected_resume_id_for_campaign_candidate():
    service, candidate, campaign, resume, campaign_candidate_repo, audit_service, celery_task_log_service = (
        _setup_happy_path()
    )

    with patch("app.services.talent_pool.talent_pool_service._enqueue_resume_embedding"):
        service.add_candidate_to_campaign(candidate.id, campaign.id, actor_id="user-1")

    created_campaign_candidate = campaign_candidate_repo.create_idempotent.call_args.args[0]
    assert created_campaign_candidate.resume_id == resume.id
    assert created_campaign_candidate.campaign_id == campaign.id
    assert created_campaign_candidate.candidate_id == candidate.id
    assert created_campaign_candidate.pipeline_stage == PipelineStage.UPLOADED


def test_add_candidate_to_campaign_records_selection_metadata_on_audit_log():
    service, candidate, campaign, resume, campaign_candidate_repo, audit_service, celery_task_log_service = (
        _setup_happy_path(selection_method=SelectionMethod.COMPARED)
    )

    with patch("app.services.talent_pool.talent_pool_service._enqueue_resume_embedding"):
        service.add_candidate_to_campaign(candidate.id, campaign.id, actor_id="user-1")

    details = audit_service.log.call_args.kwargs["details"]
    assert details["selection_method"] == "COMPARED"
    assert details["selected_resume_id"] == str(resume.id)
    assert details["eligible_resume_count"] == 2


def test_add_candidate_to_campaign_queues_skill_normalize_and_embed_resume_when_parser_current():
    service, candidate, campaign, resume, campaign_candidate_repo, audit_service, celery_task_log_service = (
        _setup_happy_path(parser_version=ResumeService.PARSER_VERSION)
    )

    with patch("app.services.talent_pool.talent_pool_service._enqueue_resume_embedding") as mock_enqueue_embedding:
        result = service.add_candidate_to_campaign(candidate.id, campaign.id, actor_id="user-1", actor_role="HR_ADMIN")

    assert result.queued_task_types == ["SKILL_NORMALIZE", "EMBED_RESUME"]
    celery_task_log_service.create_log.assert_called_once()
    assert celery_task_log_service.create_log.call_args.kwargs["task_type"] == "SKILL_NORMALIZE"
    mock_enqueue_embedding.assert_called_once_with(service.resume_repo.db, resume.id, celery_task_log_service)
    campaign_candidate_repo.create_stage_history.assert_called_once()
    audit_service.log.assert_called_once()
    campaign_candidate_repo.commit.assert_called_once()


def test_add_candidate_to_campaign_requeues_resume_processing_when_parser_outdated():
    service, candidate, campaign, resume, campaign_candidate_repo, audit_service, celery_task_log_service = (
        _setup_happy_path(parser_version="an-old-parser-v0")
    )

    with patch("app.services.talent_pool.talent_pool_service.process_resume_document") as mock_process_resume:
        result = service.add_candidate_to_campaign(candidate.id, campaign.id, actor_id="user-1")

    assert result.queued_task_types == ["RESUME_DOCUMENT_PROCESSING"]
    mock_process_resume.apply_async.assert_called_once()
    service.resume_repo.set_task_id.assert_called_once()
    celery_task_log_service.repository.create_if_new_idempotency_key.assert_called_once()


def test_add_candidate_to_campaign_returns_existing_row_without_requeuing_on_idempotent_retry():
    service, candidate, campaign, resume, campaign_candidate_repo, audit_service, celery_task_log_service = (
        _setup_happy_path()
    )
    existing_campaign_candidate = _make_campaign_candidate(campaign.id, candidate.id, resume.id)
    campaign_candidate_repo.create_idempotent.return_value = (existing_campaign_candidate, False)

    result = service.add_candidate_to_campaign(candidate.id, campaign.id, actor_id="user-1")

    assert result.queued_task_types == []
    campaign_candidate_repo.create_stage_history.assert_not_called()
    audit_service.log.assert_not_called()
    celery_task_log_service.create_log.assert_not_called()


def test_add_candidate_to_campaign_rolls_back_on_audit_failure():
    service, candidate, campaign, resume, campaign_candidate_repo, audit_service, celery_task_log_service = (
        _setup_happy_path()
    )
    audit_service.log.side_effect = RuntimeError("audit backend unavailable")

    with pytest.raises(RuntimeError):
        service.add_candidate_to_campaign(candidate.id, campaign.id, actor_id="user-1")

    campaign_candidate_repo.rollback.assert_called_once()
    campaign_candidate_repo.commit.assert_not_called()
