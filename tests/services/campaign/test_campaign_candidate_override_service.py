from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from app.exceptions.campaign_exceptions import CampaignException
from app.models.async_tasks import TaskStatus
from app.models.pipeline import AIEvaluationStatus, DecisionSource, DecisionType, PipelineStage
from app.schemas.campaign.campaign_candidate_schema import CandidateScorecardResponse
from app.services.campaign.campaign_candidate_service import CampaignCandidateService
from app.tasks.ai_evaluation_tasks import AI_EVALUATE_TASK_TYPE

"""
M07-E03 S04 - Allow an HR override on a deterministic (or semantic) rejection.

Since the decision_* redesign (98d721e) a rejection is a decision on the
candidate (decision_type REJECTED + decision_source/reason/at) plus a REJECTED
stage-history row; the override is a RESET decision applied by
StageTransitionService.apply_hr_override, which keeps the original rejection in
decision_details (overridden_decision_*).

T01/T02: apply_hr_override (validation gates, apply, re-queue)
T03: get_override_report / export_override_report

No real Celery task is ever enqueued here: AI-evaluation apply_async and the
semantic-scoring enqueue are patched for every test.
"""

_OVERRIDE_REASON = "This rejection was a mistake, overriding."


@pytest.fixture(autouse=True)
def no_task_enqueue():
    with patch("app.tasks.ai_evaluation_tasks.calculate_ai_evaluation_task.apply_async") as ai_apply_async, \
            patch("app.services.campaign.campaign_candidate_service._enqueue_semantic_scoring") as semantic_enqueue:
        yield SimpleNamespace(ai_apply_async=ai_apply_async, semantic_enqueue=semantic_enqueue)


def _make_campaign_candidate(pipeline_stage=PipelineStage.REJECTED, deterministic_score=35.0, campaign_id=None):
    return SimpleNamespace(
        id=uuid4(),
        campaign_id=campaign_id or uuid4(),
        candidate_id=uuid4(),
        resume_id=uuid4(),
        pipeline_stage=pipeline_stage,
        deterministic_breakdown={},
        semantic_breakdown=None, semantic_passed=None, previous_stage=None,
        decision_type=None, decision_source=None, decision_reason=None, decision_details=None,
        decision_by_user_id=None, decision_at=None,
        ai_evaluation=None,
        deterministic_passed=False,
        deterministic_score=deterministic_score,
        semantic_score=None,
        composite_score=None,
        created_at=datetime.now(timezone.utc),
    )


def _reject(candidate, source=DecisionSource.DETERMINISTIC, reason="Missing required skills: Python."):
    """A rejection as StageTransitionService.transition_to_rejected records it."""
    candidate.pipeline_stage = PipelineStage.REJECTED
    candidate.decision_type = DecisionType.REJECTED
    candidate.decision_source = source
    candidate.decision_reason = reason
    candidate.decision_at = datetime.now(timezone.utc)
    return candidate


def _apply_reset(candidate, changed_by, decision_reason):
    """What StageTransitionService.apply_hr_override does to the candidate (RESET, original kept in details)."""
    candidate.decision_details = {
        "overridden_decision_type": candidate.decision_type.value if candidate.decision_type else None,
        "overridden_decision_source": candidate.decision_source.value if candidate.decision_source else None,
        "overridden_decision_reason": candidate.decision_reason,
        "overridden_decision_at": candidate.decision_at.isoformat() if candidate.decision_at else None,
    }
    candidate.previous_stage = candidate.pipeline_stage
    candidate.pipeline_stage = PipelineStage.SCREENING
    candidate.decision_type = DecisionType.RESET
    candidate.decision_source = DecisionSource.RECRUITER
    candidate.decision_reason = decision_reason
    candidate.decision_by_user_id = changed_by
    candidate.decision_at = datetime.now(timezone.utc)


def _overridden(campaign_id, reason="Approved after manual review", by=None):
    candidate = _reject(_make_campaign_candidate(campaign_id=campaign_id))
    _apply_reset(candidate, changed_by=by, decision_reason=reason)
    return candidate


def _rejection_row(reason):
    return SimpleNamespace(id=uuid4(), to_stage=PipelineStage.REJECTED, changed_at=datetime.now(timezone.utc),
                           scores_snapshot={"decision_source": "DETERMINISTIC", "decision_reason": reason})


def make_service(
    campaign_candidate_repo=None,
    campaign_repo=None,
    audit_service=None,
    stage_transition_service=None,
    config_repo=None,
    celery_task_log_service=None,
    resume_repo=None,
    ai_evaluation_repo=None,
):
    return CampaignCandidateService(
        campaign_repo=campaign_repo or MagicMock(),
        campaign_candidate_repo=campaign_candidate_repo or MagicMock(),
        audit_service=audit_service or MagicMock(),
        stage_transition_service=stage_transition_service,
        config_repo=config_repo,
        celery_task_log_service=celery_task_log_service,
        resume_repo=resume_repo,
        ai_evaluation_repo=ai_evaluation_repo or MagicMock(),
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


def test_apply_hr_override_requires_a_rejection_decision_to_exist():
    candidate = _make_campaign_candidate(pipeline_stage=PipelineStage.REJECTED)   # no rejection recorded
    campaign_candidate_repo = MagicMock()
    campaign_candidate_repo.get_by_id.return_value = candidate
    service = make_service(campaign_candidate_repo=campaign_candidate_repo)

    with pytest.raises(CampaignException) as exc_info:
        service.apply_hr_override(candidate.id, "a reason that is at least twenty chars", "hr-1", "HR_ADMIN")

    assert exc_info.value.status_code == 409


def test_apply_hr_override_rejects_non_deterministic_or_semantic_rejection_layer():
    """DETERMINISTIC and SEMANTIC rejections may be overridden - an AI rejection may not."""
    candidate = _reject(_make_campaign_candidate(), source=DecisionSource.AI, reason="AI evaluation gaps.")
    campaign_candidate_repo = MagicMock()
    campaign_candidate_repo.get_by_id.return_value = candidate
    service = make_service(campaign_candidate_repo=campaign_candidate_repo)

    with pytest.raises(CampaignException) as exc_info:
        service.apply_hr_override(candidate.id, "a reason that is at least twenty chars", "hr-1", "HR_ADMIN")

    assert exc_info.value.status_code == 409


def test_apply_hr_override_allows_semantic_rejection_layer():
    service, candidate, _, _, _, _, _ = _make_applying_harness(source=DecisionSource.SEMANTIC)

    result = service.apply_hr_override(candidate.id, "a reason that is at least twenty chars", "hr-1", "HR_ADMIN")

    assert isinstance(result, CandidateScorecardResponse)


# ----------------------------------------------------------------------
# T02 - Apply Override & Re-enter Candidate Into Pipeline
# ----------------------------------------------------------------------

def _make_applying_harness(embedding_exists=False, stage_transition_result=True,
                           source=DecisionSource.DETERMINISTIC, semantic_score=None):
    candidate = _reject(_make_campaign_candidate(), source=source, reason="Missing required skills: Python.")
    candidate.semantic_score = semantic_score
    original_reason = candidate.decision_reason

    campaign_candidate_repo = MagicMock()
    campaign_candidate_repo.get_by_id.return_value = candidate
    audit_service = MagicMock()

    def apply_hr_override(cc, changed_by, change_reason, decision_reason=None):
        if stage_transition_result:
            _apply_reset(cc, changed_by, decision_reason)
        return stage_transition_result
    stage_transition_service = MagicMock()
    stage_transition_service.apply_hr_override.side_effect = apply_hr_override

    celery_task_log_service = MagicMock()
    celery_task_log_repo = MagicMock()
    celery_task_log_repo.get_by_campaign_candidate_and_task_type.return_value = []
    celery_task_log_service.repository = celery_task_log_repo

    resume_repo = MagicMock()
    resume_repo.get_by_id.return_value = None  # the scorecard rebuilt after the override needs no real resume
    resume_repo.get_embedding.return_value = SimpleNamespace(id=uuid4()) if embedding_exists else None

    ai_evaluation = SimpleNamespace(ai_evaluation_status=None)
    ai_evaluation_repo = MagicMock()
    ai_evaluation_repo.get_or_create.return_value = ai_evaluation

    service = make_service(
        campaign_candidate_repo=campaign_candidate_repo,
        audit_service=audit_service,
        stage_transition_service=stage_transition_service,
        celery_task_log_service=celery_task_log_service,
        resume_repo=resume_repo,
        ai_evaluation_repo=ai_evaluation_repo,
    )
    service._test_ai_evaluation = ai_evaluation
    return (service, candidate, original_reason, campaign_candidate_repo, audit_service,
            stage_transition_service, celery_task_log_service)


def test_apply_hr_override_sets_campaign_candidate_fields_and_commits():
    service, candidate, original_reason, campaign_candidate_repo, audit_service, stage_transition_service, _ = (
        _make_applying_harness())

    result = service.apply_hr_override(candidate.id, _OVERRIDE_REASON, "hr-1", "HR_ADMIN")

    assert isinstance(result, CandidateScorecardResponse)
    assert candidate.deterministic_passed is True
    assert service._test_ai_evaluation.ai_evaluation_status == AIEvaluationStatus.PENDING
    stage_transition_service.apply_hr_override.assert_called_once_with(
        candidate, changed_by="hr-1", change_reason="RECRUITER override of deterministic rejection",
        decision_reason=_OVERRIDE_REASON,
    )
    assert candidate.decision_type == DecisionType.RESET          # applied by the stage transition
    campaign_candidate_repo.commit.assert_called_once()
    campaign_candidate_repo.rollback.assert_not_called()

    audit_service.log.assert_called_once()
    _, kwargs = audit_service.log.call_args
    assert kwargs["action_type"].value == "DETERMINISTIC_OVERRIDE_APPLIED"
    assert kwargs["details"]["override_reason"] == _OVERRIDE_REASON
    assert kwargs["details"]["original_rejection_reason"] == original_reason
    assert kwargs["details"]["overridden_layer"] == "DETERMINISTIC"


def test_apply_hr_override_rolls_back_and_never_commits_when_transition_blocked():
    service, candidate, _, campaign_candidate_repo, audit_service, _, _ = _make_applying_harness(
        stage_transition_result=False)

    with pytest.raises(CampaignException) as exc_info:
        service.apply_hr_override(candidate.id, _OVERRIDE_REASON, "hr-1", "HR_ADMIN")

    assert exc_info.value.status_code == 409
    campaign_candidate_repo.commit.assert_not_called()
    campaign_candidate_repo.rollback.assert_called_once()
    audit_service.log.assert_not_called()


def test_apply_hr_override_does_not_queue_ai_evaluate_upfront_when_semantic_score_missing(no_task_enqueue):
    """A deterministic-layer override (no semantic score yet) re-runs semantic scoring first, not AI."""
    service, candidate, _, _, _, _, celery_task_log_service = _make_applying_harness(semantic_score=None)

    service.apply_hr_override(candidate.id, _OVERRIDE_REASON, "hr-1", "HR_ADMIN")

    created_task_types = [call.kwargs["task_type"] for call in celery_task_log_service.create_log.call_args_list]
    assert AI_EVALUATE_TASK_TYPE not in created_task_types
    no_task_enqueue.semantic_enqueue.assert_called_once()
    no_task_enqueue.ai_apply_async.assert_not_called()


def test_apply_hr_override_queues_ai_evaluate_immediately_when_semantic_score_already_exists(no_task_enqueue):
    """A semantic-layer override already has a semantic score: AI_EVALUATE is queued, semantic is not re-run."""
    service, candidate, _, _, _, _, celery_task_log_service = _make_applying_harness(
        source=DecisionSource.SEMANTIC, semantic_score=0.42)

    service.apply_hr_override(candidate.id, _OVERRIDE_REASON, "hr-1", "HR_ADMIN")

    created_task_types = [call.kwargs["task_type"] for call in celery_task_log_service.create_log.call_args_list]
    assert created_task_types == [AI_EVALUATE_TASK_TYPE]
    for call in celery_task_log_service.create_log.call_args_list:
        assert call.kwargs["campaign_candidate_id"] == candidate.id
    no_task_enqueue.semantic_enqueue.assert_not_called()
    no_task_enqueue.ai_apply_async.assert_called_once()        # patched: nothing reached a broker


def test_apply_hr_override_never_enqueues_composite_scoring_directly():
    """
    An HR override only restarts the remaining scoring pipeline - never composite
    scoring directly (its only triggers are AI evaluation completing and a
    campaign weight change). Locks in that campaign_candidate_service no longer
    even imports a composite enqueue.
    """
    import app.services.campaign.campaign_candidate_service as module

    assert not hasattr(module, "_enqueue_composite_scoring")


def test_apply_hr_override_does_not_enqueue_duplicate_ai_evaluate(no_task_enqueue):
    service, candidate, _, _, _, _, celery_task_log_service = _make_applying_harness(
        source=DecisionSource.SEMANTIC, semantic_score=0.42)
    celery_task_log_service.repository.get_by_campaign_candidate_and_task_type.return_value = [
        SimpleNamespace(status=TaskStatus.QUEUED),
    ]

    service.apply_hr_override(candidate.id, _OVERRIDE_REASON, "hr-1", "HR_ADMIN")

    celery_task_log_service.create_log.assert_not_called()
    no_task_enqueue.ai_apply_async.assert_not_called()


def test_apply_hr_override_queuing_failure_never_raises_or_undoes_the_override():
    service, candidate, _, campaign_candidate_repo, _, _, celery_task_log_service = _make_applying_harness(
        source=DecisionSource.SEMANTIC, semantic_score=0.42)
    celery_task_log_service.create_log.side_effect = Exception("broker unreachable")

    # Must not raise even though queuing blew up - the override itself already committed.
    result = service.apply_hr_override(candidate.id, _OVERRIDE_REASON, "hr-1", "HR_ADMIN")

    assert isinstance(result, CandidateScorecardResponse)
    campaign_candidate_repo.commit.assert_called_once()


# ----------------------------------------------------------------------
# T03 - Override Report
# ----------------------------------------------------------------------

def _report_repos(campaign, overridden, rejected_count, history=None):
    campaign_candidate_repo = MagicMock()
    campaign_candidate_repo.get_overridden.return_value = overridden
    campaign_candidate_repo.count_rejected_by_campaigns.return_value = {campaign.id: rejected_count} if campaign else {}
    campaign_candidate_repo.get_stage_history_by_campaign_candidate_id.return_value = history or []
    campaign_repo = MagicMock()
    campaign_repo.get_by_id.return_value = campaign
    campaign_repo.get_hiring_manager_names.return_value = {}
    return campaign_candidate_repo, campaign_repo


def test_get_override_report_builds_rows_and_resolves_names():
    campaign = SimpleNamespace(id=uuid4(), name="Backend Engineer Q3")
    candidate = _overridden(campaign.id, reason="Approved after manual review", by="hr-1")
    campaign_candidate_repo, campaign_repo = _report_repos(
        campaign, [candidate], rejected_count=1, history=[_rejection_row("Missing required skills: Python.")])
    campaign_repo.get_hiring_manager_names.return_value = {"hr-1": "Jordan HR"}

    service = make_service(campaign_candidate_repo=campaign_candidate_repo, campaign_repo=campaign_repo)

    report = service.get_override_report(campaign_id=campaign.id)

    assert report.total_count == 1
    row = report.rows[0]
    assert row.campaign_id == campaign.id
    assert row.campaign_name == "Backend Engineer Q3"
    assert row.candidate_uuid == candidate.candidate_id
    assert row.original_rejection_reason == "Missing required skills: Python."
    assert row.override_reason == "Approved after manual review"
    assert row.hr_full_name == "Jordan HR"
    assert row.current_pipeline_stage == PipelineStage.SCREENING
    # Never includes candidate name/email/phone/resume.
    assert not hasattr(row, "candidate_name")


def test_get_override_report_weekly_trend_has_eight_buckets_newest_last():
    campaign_candidate_repo, _ = _report_repos(None, [], rejected_count=0)
    service = make_service(campaign_candidate_repo=campaign_candidate_repo)

    report = service.get_override_report()

    assert len(report.weekly_trend) == 8
    weeks = [point.week_start for point in report.weekly_trend]
    assert weeks == sorted(weeks)
    assert all(point.override_count == 0 for point in report.weekly_trend)


def test_get_override_report_alert_triggers_above_threshold():
    campaign = SimpleNamespace(id=uuid4(), name="Data Scientist")
    overridden = [_overridden(campaign.id, reason="reason") for _ in range(3)]
    # 3 overrides out of 5 rejected -> 60% override rate, above the 20% threshold.
    campaign_candidate_repo, campaign_repo = _report_repos(campaign, overridden, rejected_count=5)
    config_repo = MagicMock()
    config_repo.get_configs_by_keys.return_value = {"OVERRIDE_RATE_ALERT_THRESHOLD": "20"}

    service = make_service(campaign_candidate_repo=campaign_candidate_repo, campaign_repo=campaign_repo,
                           config_repo=config_repo)

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
    # 1 override out of 20 rejected -> 5%, below the 20% default threshold.
    campaign_candidate_repo, campaign_repo = _report_repos(campaign, [_overridden(campaign.id, reason="reason")],
                                                           rejected_count=20)
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
    candidate = _overridden(campaign.id, reason="Approved after manual review")
    campaign_candidate_repo, campaign_repo = _report_repos(
        campaign, [candidate], rejected_count=1, history=[_rejection_row("Missing required skills: Python.")])
    audit_service = MagicMock()

    service = make_service(campaign_candidate_repo=campaign_candidate_repo, campaign_repo=campaign_repo,
                           audit_service=audit_service)

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
    assert data_row[2] == "Missing required skills: Python."
    assert data_row[3] == "Approved after manual review"

    # Never exports PII.
    for forbidden in ("candidate_name", "email", "phone", "resume"):
        assert forbidden not in header

    audit_service.log.assert_called_once()
    _, kwargs = audit_service.log.call_args
    assert kwargs["action_type"].value == "OVERRIDE_REPORT_EXPORTED"
    assert kwargs["entity_id"] == campaign.id


def test_export_override_report_uses_sentinel_entity_id_when_no_campaign_filter():
    campaign_candidate_repo, _ = _report_repos(None, [], rejected_count=0)
    audit_service = MagicMock()

    service = make_service(campaign_candidate_repo=campaign_candidate_repo, audit_service=audit_service)

    service.export_override_report(campaign_id=None, date_from=None, date_to=None, actor_id="hr-1", actor_role="HR_ADMIN")

    _, kwargs = audit_service.log.call_args
    assert kwargs["entity_id"] == CampaignCandidateService.EXPORT_AUDIT_ENTITY_ID
    assert kwargs["campaign_id"] is None
