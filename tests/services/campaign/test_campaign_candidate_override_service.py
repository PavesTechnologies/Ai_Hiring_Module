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
T03: (the Override Report was retired 2026-11-02 - see test_outcome_overrides.py)

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
    assert kwargs["details"]["override_reason_provided"] is True           # free text stays on the row, not in the audit
    assert _OVERRIDE_REASON not in str(kwargs["details"])
    assert kwargs["details"]["original_rejection_reason_provided"] is bool(original_reason)
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
