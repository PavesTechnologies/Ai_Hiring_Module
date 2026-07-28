from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from app.exceptions.campaign_exceptions import CampaignException
from app.models.async_tasks import TaskStatus
from app.models.pipeline import PipelineStage, RejectionLayer
from app.schemas.campaign.campaign_candidate_schema import CandidateScorecardResponse
from app.services.campaign.campaign_candidate_service import (
    AI_EVALUATE_TASK_TYPE,
    SEMANTIC_SCORE_TASK_TYPE,
    CampaignCandidateService,
)

"""
M07-E03 S04 - Allow HR_ADMIN Override on Deterministic Rejection.

T01/T02: apply_hr_override (initiation validation + apply + re-queue)
T03: get_override_report / export_override_report
"""


def _make_campaign_candidate(
    pipeline_stage=PipelineStage.REJECTED,
    score_breakdown=None,
    deterministic_score=35.0,
    hr_override=False,
    campaign_id=None,
):
    return SimpleNamespace(
        id=uuid4(),
        campaign_id=campaign_id or uuid4(),
        candidate_id=uuid4(),
        resume_id=uuid4(),
        pipeline_stage=pipeline_stage,
        score_breakdown=score_breakdown or {},
        hr_override=hr_override,
        hr_override_reason=None,
        hr_override_by=None,
        hr_override_at=None,
        deterministic_passed=False,
        ai_evaluation_status=None,
        deterministic_score=deterministic_score,
        ai_ats_score=None,
        semantic_score=None,
        composite_score=None,
        created_at=datetime.now(timezone.utc),
    )


def _make_rejection(rejection_layer=RejectionLayer.DETERMINISTIC, reason="Missing required skills: Python.", rejected_at=None):
    return SimpleNamespace(
        id=uuid4(),
        rejection_layer=rejection_layer,
        rejection_reason=reason,
        rejected_at=rejected_at or datetime.now(timezone.utc),
    )


def make_service(
    campaign_candidate_repo=None,
    candidate_rejection_repo=None,
    campaign_repo=None,
    audit_service=None,
    stage_transition_service=None,
    config_repo=None,
    celery_task_log_service=None,
    resume_repo=None,
):
    return CampaignCandidateService(
        campaign_repo=campaign_repo or MagicMock(),
        campaign_candidate_repo=campaign_candidate_repo or MagicMock(),
        audit_service=audit_service or MagicMock(),
        candidate_rejection_repo=candidate_rejection_repo,
        stage_transition_service=stage_transition_service,
        config_repo=config_repo,
        celery_task_log_service=celery_task_log_service,
        resume_repo=resume_repo,
    )


# ----------------------------------------------------------------------
# T01 - Initiate HR Override (validation gates before applying)
# ----------------------------------------------------------------------

def test_apply_hr_override_not_found():
    campaign_candidate_repo = MagicMock()
    campaign_candidate_repo.get_by_id.return_value = None
    service = make_service(campaign_candidate_repo=campaign_candidate_repo)

    with pytest.raises(CampaignException) as exc_info:
        service.apply_hr_override(uuid4(), "a reason that is at least twenty chars", "hr-1", "HR_ADMIN")

    assert exc_info.value.status_code == 404


def test_apply_hr_override_requires_rejected_stage():
    candidate = _make_campaign_candidate(pipeline_stage=PipelineStage.SCREENING)
    campaign_candidate_repo = MagicMock()
    campaign_candidate_repo.get_by_id.return_value = candidate
    service = make_service(campaign_candidate_repo=campaign_candidate_repo)

    with pytest.raises(CampaignException) as exc_info:
        service.apply_hr_override(candidate.id, "a reason that is at least twenty chars", "hr-1", "HR_ADMIN")

    assert exc_info.value.status_code == 409
    campaign_candidate_repo.rollback.assert_called_once()


def test_apply_hr_override_requires_a_deterministic_rejection_to_exist():
    candidate = _make_campaign_candidate(pipeline_stage=PipelineStage.REJECTED)
    campaign_candidate_repo = MagicMock()
    campaign_candidate_repo.get_by_id.return_value = candidate
    candidate_rejection_repo = MagicMock()
    candidate_rejection_repo.get_by_campaign_candidate_id.return_value = []  # no rejection on record

    service = make_service(campaign_candidate_repo=campaign_candidate_repo, candidate_rejection_repo=candidate_rejection_repo)

    with pytest.raises(CampaignException) as exc_info:
        service.apply_hr_override(candidate.id, "a reason that is at least twenty chars", "hr-1", "HR_ADMIN")

    assert exc_info.value.status_code == 409


def test_apply_hr_override_rejects_non_deterministic_rejection_layer():
    candidate = _make_campaign_candidate(pipeline_stage=PipelineStage.REJECTED)
    campaign_candidate_repo = MagicMock()
    campaign_candidate_repo.get_by_id.return_value = candidate
    candidate_rejection_repo = MagicMock()
    candidate_rejection_repo.get_by_campaign_candidate_id.return_value = [
        _make_rejection(rejection_layer=RejectionLayer.SEMANTIC),
    ]

    service = make_service(campaign_candidate_repo=campaign_candidate_repo, candidate_rejection_repo=candidate_rejection_repo)

    with pytest.raises(CampaignException) as exc_info:
        service.apply_hr_override(candidate.id, "a reason that is at least twenty chars", "hr-1", "HR_ADMIN")

    assert exc_info.value.status_code == 409


# ----------------------------------------------------------------------
# T02 - Apply Override & Re-enter Candidate Into Pipeline
# ----------------------------------------------------------------------

def _make_applying_harness(embedding_exists=False, stage_transition_result=True):
    candidate = _make_campaign_candidate(pipeline_stage=PipelineStage.REJECTED)
    rejection = _make_rejection(reason="Missing required skills: Python.")

    campaign_candidate_repo = MagicMock()
    campaign_candidate_repo.get_by_id.return_value = candidate
    candidate_rejection_repo = MagicMock()
    candidate_rejection_repo.get_by_campaign_candidate_id.return_value = [rejection]
    audit_service = MagicMock()
    stage_transition_service = MagicMock()
    stage_transition_service.apply_hr_override.return_value = stage_transition_result

    celery_task_log_service = MagicMock()
    celery_task_log_repo = MagicMock()
    celery_task_log_repo.get_by_campaign_candidate_and_task_type.return_value = []
    celery_task_log_service.repository = celery_task_log_repo

    resume_repo = MagicMock()
    resume_repo.get_by_id.return_value = None  # scorecard rebuild after override doesn't need a real resume
    resume_repo.get_embedding.return_value = SimpleNamespace(id=uuid4()) if embedding_exists else None

    service = make_service(
        campaign_candidate_repo=campaign_candidate_repo,
        candidate_rejection_repo=candidate_rejection_repo,
        audit_service=audit_service,
        stage_transition_service=stage_transition_service,
        celery_task_log_service=celery_task_log_service,
        resume_repo=resume_repo,
    )
    return service, candidate, rejection, campaign_candidate_repo, audit_service, stage_transition_service, celery_task_log_service


def test_apply_hr_override_sets_campaign_candidate_fields_and_commits():
    service, candidate, rejection, campaign_candidate_repo, audit_service, stage_transition_service, _ = _make_applying_harness()

    result = service.apply_hr_override(candidate.id, "This rejection was a mistake, overriding.", "hr-1", "HR_ADMIN")

    assert isinstance(result, CandidateScorecardResponse)
    assert candidate.hr_override is True
    assert candidate.hr_override_reason == "This rejection was a mistake, overriding."
    assert candidate.hr_override_by == "hr-1"
    assert candidate.hr_override_at is not None
    assert candidate.deterministic_passed is True
    from app.models.pipeline import AIEvaluationStatus
    assert candidate.ai_evaluation_status == AIEvaluationStatus.PENDING

    stage_transition_service.apply_hr_override.assert_called_once_with(
        candidate, changed_by="hr-1", change_reason="HR_ADMIN override of deterministic rejection",
    )
    campaign_candidate_repo.commit.assert_called_once()
    campaign_candidate_repo.rollback.assert_not_called()

    audit_service.log.assert_called_once()
    _, kwargs = audit_service.log.call_args
    assert kwargs["action_type"].value == "DETERMINISTIC_OVERRIDE_APPLIED"
    assert kwargs["details"]["original_rejection_reason"] == rejection.rejection_reason


def test_apply_hr_override_rolls_back_and_never_commits_when_transition_blocked():
    service, candidate, rejection, campaign_candidate_repo, audit_service, stage_transition_service, _ = _make_applying_harness(
        stage_transition_result=False,
    )

    with pytest.raises(CampaignException) as exc_info:
        service.apply_hr_override(candidate.id, "This rejection was a mistake, overriding.", "hr-1", "HR_ADMIN")

    assert exc_info.value.status_code == 409
    campaign_candidate_repo.commit.assert_not_called()
    campaign_candidate_repo.rollback.assert_called_once()
    audit_service.log.assert_not_called()


def test_apply_hr_override_queues_ai_evaluate_only_when_no_embedding():
    service, candidate, _, _, _, _, celery_task_log_service = _make_applying_harness(embedding_exists=False)

    service.apply_hr_override(candidate.id, "This rejection was a mistake, overriding.", "hr-1", "HR_ADMIN")

    created_task_types = [call.kwargs["task_type"] for call in celery_task_log_service.create_log.call_args_list]
    assert created_task_types == [AI_EVALUATE_TASK_TYPE]


def test_apply_hr_override_queues_ai_evaluate_and_semantic_score_when_embedding_exists():
    service, candidate, _, _, _, _, celery_task_log_service = _make_applying_harness(embedding_exists=True)

    service.apply_hr_override(candidate.id, "This rejection was a mistake, overriding.", "hr-1", "HR_ADMIN")

    created_task_types = [call.kwargs["task_type"] for call in celery_task_log_service.create_log.call_args_list]
    assert created_task_types == [AI_EVALUATE_TASK_TYPE, SEMANTIC_SCORE_TASK_TYPE]
    for call in celery_task_log_service.create_log.call_args_list:
        assert call.kwargs["campaign_candidate_id"] == candidate.id


def test_apply_hr_override_does_not_enqueue_duplicate_ai_evaluate():
    service, candidate, _, _, _, _, celery_task_log_service = _make_applying_harness(embedding_exists=False)
    celery_task_log_service.repository.get_by_campaign_candidate_and_task_type.return_value = [
        SimpleNamespace(status=TaskStatus.QUEUED),
    ]

    service.apply_hr_override(candidate.id, "This rejection was a mistake, overriding.", "hr-1", "HR_ADMIN")

    celery_task_log_service.create_log.assert_not_called()


def test_apply_hr_override_queuing_failure_never_raises_or_undoes_the_override():
    service, candidate, _, campaign_candidate_repo, _, _, celery_task_log_service = _make_applying_harness()
    celery_task_log_service.create_log.side_effect = Exception("broker unreachable")

    # Must not raise even though queuing blew up - the override itself already committed.
    result = service.apply_hr_override(candidate.id, "This rejection was a mistake, overriding.", "hr-1", "HR_ADMIN")

    assert isinstance(result, CandidateScorecardResponse)
    campaign_candidate_repo.commit.assert_called_once()


# ----------------------------------------------------------------------
# T03 - Override Report
# ----------------------------------------------------------------------

def test_get_override_report_builds_rows_and_resolves_names():
    campaign = SimpleNamespace(id=uuid4(), name="Backend Engineer Q3")
    candidate = _make_campaign_candidate(pipeline_stage=PipelineStage.SCREENING, hr_override=True, campaign_id=campaign.id)
    candidate.hr_override_reason = "Approved after manual review"
    candidate.hr_override_by = "hr-1"
    candidate.hr_override_at = datetime.now(timezone.utc)
    original_rejection = _make_rejection(reason="Missing required skills: Python.")

    campaign_candidate_repo = MagicMock()
    campaign_candidate_repo.get_overridden.return_value = [candidate]
    campaign_candidate_repo.get_rejected_by_campaign.return_value = [candidate]
    campaign_repo = MagicMock()
    campaign_repo.get_by_id.return_value = campaign
    campaign_repo.get_hiring_manager_names.return_value = {"hr-1": "Jordan HR"}
    candidate_rejection_repo = MagicMock()
    candidate_rejection_repo.get_by_campaign_candidate_id.return_value = [original_rejection]

    service = make_service(
        campaign_candidate_repo=campaign_candidate_repo,
        campaign_repo=campaign_repo,
        candidate_rejection_repo=candidate_rejection_repo,
    )

    report = service.get_override_report(campaign_id=campaign.id)

    assert report.total_count == 1
    row = report.rows[0]
    assert row.campaign_id == campaign.id
    assert row.campaign_name == "Backend Engineer Q3"
    assert row.candidate_uuid == candidate.candidate_id
    assert row.original_rejection_reason == original_rejection.rejection_reason
    assert row.override_reason == "Approved after manual review"
    assert row.hr_full_name == "Jordan HR"
    assert row.current_pipeline_stage == PipelineStage.SCREENING
    # Never includes candidate name/email/phone/resume.
    assert not hasattr(row, "candidate_name")


def test_get_override_report_weekly_trend_has_eight_buckets_newest_last():
    campaign_candidate_repo = MagicMock()
    campaign_candidate_repo.get_overridden.return_value = []
    campaign_candidate_repo.get_rejected_by_campaign.return_value = []

    service = make_service(campaign_candidate_repo=campaign_candidate_repo)

    report = service.get_override_report()

    assert len(report.weekly_trend) == 8
    weeks = [point.week_start for point in report.weekly_trend]
    assert weeks == sorted(weeks)
    assert all(point.override_count == 0 for point in report.weekly_trend)


def test_get_override_report_alert_triggers_above_threshold():
    campaign = SimpleNamespace(id=uuid4(), name="Data Scientist")
    overridden = [_make_campaign_candidate(pipeline_stage=PipelineStage.SCREENING, hr_override=True, campaign_id=campaign.id) for _ in range(3)]
    for cc in overridden:
        cc.hr_override_reason = "reason"
        cc.hr_override_by = None
        cc.hr_override_at = datetime.now(timezone.utc)

    campaign_candidate_repo = MagicMock()
    campaign_candidate_repo.get_overridden.return_value = overridden
    # 3 overrides out of 5 rejected -> 60% override rate, above default/threshold.
    campaign_candidate_repo.get_rejected_by_campaign.return_value = list(range(5))
    campaign_repo = MagicMock()
    campaign_repo.get_by_id.return_value = campaign
    campaign_repo.get_hiring_manager_names.return_value = {}
    config_repo = MagicMock()
    config_repo.get_configs_by_keys.return_value = {"OVERRIDE_RATE_ALERT_THRESHOLD": "20"}

    service = make_service(
        campaign_candidate_repo=campaign_candidate_repo,
        campaign_repo=campaign_repo,
        config_repo=config_repo,
    )

    report = service.get_override_report(campaign_id=campaign.id)

    assert len(report.campaign_alerts) == 1
    alert = report.campaign_alerts[0]
    assert alert.override_count == 3
    assert alert.rejected_count == 5
    assert alert.override_rate == 60.0
    assert alert.override_alert is True
    assert alert.recommendation == "Review campaign JD skills or thresholds."


def test_get_override_report_no_alert_below_threshold():
    campaign = SimpleNamespace(id=uuid4(), name="Data Scientist")
    overridden = [_make_campaign_candidate(pipeline_stage=PipelineStage.SCREENING, hr_override=True, campaign_id=campaign.id)]
    overridden[0].hr_override_reason = "reason"
    overridden[0].hr_override_by = None
    overridden[0].hr_override_at = datetime.now(timezone.utc)

    campaign_candidate_repo = MagicMock()
    campaign_candidate_repo.get_overridden.return_value = overridden
    # 1 override out of 20 rejected -> 5%, below the 20% default threshold.
    campaign_candidate_repo.get_rejected_by_campaign.return_value = list(range(20))
    campaign_repo = MagicMock()
    campaign_repo.get_by_id.return_value = campaign
    campaign_repo.get_hiring_manager_names.return_value = {}

    service = make_service(campaign_candidate_repo=campaign_candidate_repo, campaign_repo=campaign_repo)

    report = service.get_override_report(campaign_id=campaign.id)

    alert = report.campaign_alerts[0]
    assert alert.override_alert is False
    assert alert.recommendation is None


def test_export_override_report_produces_a_real_workbook_and_logs_audit():
    import asyncio
    import io

    import openpyxl

    campaign = SimpleNamespace(id=uuid4(), name="Backend Engineer Q3")
    candidate = _make_campaign_candidate(pipeline_stage=PipelineStage.SCREENING, hr_override=True, campaign_id=campaign.id)
    candidate.hr_override_reason = "Approved after manual review"
    candidate.hr_override_by = None
    candidate.hr_override_at = datetime.now(timezone.utc)

    campaign_candidate_repo = MagicMock()
    campaign_candidate_repo.get_overridden.return_value = [candidate]
    campaign_candidate_repo.get_rejected_by_campaign.return_value = [candidate]
    campaign_repo = MagicMock()
    campaign_repo.get_by_id.return_value = campaign
    campaign_repo.get_hiring_manager_names.return_value = {}
    candidate_rejection_repo = MagicMock()
    candidate_rejection_repo.get_by_campaign_candidate_id.return_value = [_make_rejection()]
    audit_service = MagicMock()

    service = make_service(
        campaign_candidate_repo=campaign_candidate_repo,
        campaign_repo=campaign_repo,
        candidate_rejection_repo=candidate_rejection_repo,
        audit_service=audit_service,
    )

    response = service.export_override_report(
        campaign_id=campaign.id, date_from=None, date_to=None, actor_id="hr-1", actor_role="HR_ADMIN",
    )

    async def _drain():
        chunks = []
        async for chunk in response.body_iterator:
            chunks.append(chunk)
        return b"".join(chunks)

    body = asyncio.run(_drain())
    workbook = openpyxl.load_workbook(io.BytesIO(body))
    sheet = workbook.active

    header = [cell.value for cell in sheet[1]]
    assert header == [
        "Campaign Name",
        "Candidate UUID",
        "Original Rejection Reason",
        "Override Reason",
        "HR Full Name",
        "Override Timestamp",
        "Current Pipeline Stage",
    ]
    data_row = [cell.value for cell in sheet[2]]
    assert data_row[0] == "Backend Engineer Q3"
    assert data_row[1] == str(candidate.candidate_id)
    assert data_row[3] == "Approved after manual review"

    # Never exports PII.
    for forbidden in ("candidate_name", "email", "phone", "resume"):
        assert forbidden not in header

    audit_service.log.assert_called_once()
    _, kwargs = audit_service.log.call_args
    assert kwargs["action_type"].value == "OVERRIDE_REPORT_EXPORTED"
    assert kwargs["entity_id"] == campaign.id


def test_export_override_report_uses_sentinel_entity_id_when_no_campaign_filter():
    campaign_candidate_repo = MagicMock()
    campaign_candidate_repo.get_overridden.return_value = []
    campaign_candidate_repo.get_rejected_by_campaign.return_value = []
    audit_service = MagicMock()

    service = make_service(campaign_candidate_repo=campaign_candidate_repo, audit_service=audit_service)

    service.export_override_report(campaign_id=None, date_from=None, date_to=None, actor_id="hr-1", actor_role="HR_ADMIN")

    _, kwargs = audit_service.log.call_args
    assert kwargs["entity_id"] == CampaignCandidateService.EXPORT_AUDIT_ENTITY_ID
    assert kwargs["campaign_id"] is None
