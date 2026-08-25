from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

from app.models.async_tasks import TaskStatus
from app.models.candidates import ParseStatus
from app.services.campaign.candidate_reevaluation_validator import ReEvaluationDecision
from app.services.campaign.manual_candidate_rescore import enqueue_manual_rescore

MODULE = "app.services.campaign.manual_candidate_rescore"

"""
Epic 5 follow-up - enqueue_manual_rescore(), fired when a candidate is
manually moved to SCREENING from anywhere other than UPLOADED. Everything
- validator checks, resume-parsed check, field resets, the actual Celery
enqueue - lives inside one try/except (same discipline as
candidate_notification_emails.py) since a failure here must never
propagate into the pipeline-stage transition that already committed.
"""


def _campaign_candidate(**overrides):
    defaults = dict(id=uuid4(), candidate_id=uuid4(), campaign_id=uuid4(), resume_id=uuid4())
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _patched(decision=None, resume=None, stale_semantic_logs=None, stale_ai_evaluate_logs=None):
    candidate_repo = MagicMock()
    campaign_repo = MagicMock()
    resume_repo = MagicMock()
    resume_repo.get_by_id.return_value = resume if resume is not None else SimpleNamespace(parse_status=ParseStatus.PARSED)
    campaign_candidate_repo = MagicMock()
    ai_evaluation_repo = MagicMock()
    ai_evaluation_repo.get_or_create.return_value = SimpleNamespace(id=uuid4())
    validator = MagicMock()
    validator.evaluate.return_value = decision or ReEvaluationDecision(True)
    celery_task_log_repo = MagicMock()
    logs_by_task_type = {
        "SEMANTIC_SCORE": stale_semantic_logs or [],
        "AI_EVALUATE": stale_ai_evaluate_logs or [],
    }
    celery_task_log_repo.get_by_campaign_candidate_and_task_type.side_effect = (
        lambda campaign_candidate_id, task_type: logs_by_task_type[task_type]
    )

    patches = [
        patch(f"{MODULE}.CandidateRepository", return_value=candidate_repo),
        patch(f"{MODULE}.CampaignRepository", return_value=campaign_repo),
        patch(f"{MODULE}.ResumeRepository", return_value=resume_repo),
        patch(f"{MODULE}.CampaignCandidateRepository", return_value=campaign_candidate_repo),
        patch(f"{MODULE}.CampaignCandidateAIEvaluationRepository", return_value=ai_evaluation_repo),
        patch(f"{MODULE}.CandidateReEvaluationValidator", return_value=validator),
        patch(f"{MODULE}.CeleryTaskLogRepository", return_value=celery_task_log_repo),
    ]
    return patches, campaign_candidate_repo, ai_evaluation_repo, celery_task_log_repo


def _run(campaign_candidate, patches, db=None):
    with ExitStack() as stack:
        mock_task = stack.enter_context(patch("app.tasks.deterministic_scoring_tasks.calculate_deterministic_score_task"))
        for p in patches:
            stack.enter_context(p)
        result = enqueue_manual_rescore(db if db is not None else MagicMock(), campaign_candidate)
    return result, mock_task


def test_queues_a_rescore_and_resets_evaluation_derived_fields():
    patches, campaign_candidate_repo, ai_evaluation_repo, celery_task_log_repo = _patched()
    campaign_candidate = _campaign_candidate()

    result, mock_task = _run(campaign_candidate, patches)

    assert result is True
    campaign_candidate_repo.reset_for_rescore.assert_called_once_with(campaign_candidate)
    ai_evaluation_repo.reset.assert_called_once()
    campaign_candidate_repo.commit.assert_called_once()
    mock_task.apply_async.assert_called_once_with(kwargs={"campaign_candidate_id": str(campaign_candidate.id)})


def test_skips_when_the_reevaluation_validator_disallows():
    patches, campaign_candidate_repo, _, _ = _patched(decision=ReEvaluationDecision(False, "Campaign status does not allow reprocessing."))
    campaign_candidate = _campaign_candidate()

    result, mock_task = _run(campaign_candidate, patches)

    assert result is False
    campaign_candidate_repo.reset_for_rescore.assert_not_called()
    mock_task.apply_async.assert_not_called()


def test_skips_when_resume_has_not_been_parsed():
    patches, campaign_candidate_repo, _, _ = _patched(resume=SimpleNamespace(parse_status=ParseStatus.PENDING))
    campaign_candidate = _campaign_candidate()

    result, mock_task = _run(campaign_candidate, patches)

    assert result is False
    campaign_candidate_repo.reset_for_rescore.assert_not_called()
    mock_task.apply_async.assert_not_called()


def test_skips_when_campaign_candidate_has_no_resume_at_all():
    patches, campaign_candidate_repo, _, _ = _patched()
    campaign_candidate = _campaign_candidate(resume_id=None)

    result, mock_task = _run(campaign_candidate, patches)

    assert result is False
    mock_task.apply_async.assert_not_called()


def test_swallows_exceptions_and_returns_false():
    patches, campaign_candidate_repo, _, _ = _patched()
    campaign_candidate_repo.reset_for_rescore.side_effect = RuntimeError("db exploded")
    campaign_candidate = _campaign_candidate()

    result, mock_task = _run(campaign_candidate, patches)

    assert result is False
    mock_task.apply_async.assert_not_called()


def test_swallows_exceptions_from_the_validator_lookup_itself():
    """Same regression class as candidate_notification_emails.py - everything must be inside the try/except, including the earliest lookups."""
    db = MagicMock()
    with patch(f"{MODULE}.CandidateRepository", side_effect=RuntimeError("boom")):
        result = enqueue_manual_rescore(db, _campaign_candidate())

    assert result is False


# ----------------------------------------------------------------------
# Bug fix - a stale SEMANTIC_SCORE/AI_EVALUATE celery_task_log row would
# otherwise silently block the deterministic task's own post-pass
# re-enqueue of semantic scoring (and, downstream, AI evaluation). Found
# live, twice, in the same investigation:
#   1. A candidate re-scored from FRAUD_REVIEW kept its prior PASSED
#      semantic breakdown forever and never got AI-evaluated - a stale
#      SUCCESS SEMANTIC_SCORE row (permanent, hash-of-candidate-id
#      idempotency key) was silently treated as "already scored."
#   2. A DIFFERENT candidate's semantic score genuinely re-ran and passed,
#      but AI evaluation still never fired - a 10-day-old AI_EVALUATE row
#      stuck at QUEUED (started_at never set - a worker never actually
#      picked it up) was silently treated as "already in flight."
# At the point this cleanup runs, nothing for the CURRENT re-score attempt
# has been dispatched yet (deterministic scoring - the only thing that can
# ever enqueue either of these two - is dispatched after this block), so
# any existing row for either task type is necessarily stale, regardless
# of its status.
# ----------------------------------------------------------------------

def test_deletes_a_stale_success_semantic_score_log_before_dispatching():
    stale_log = SimpleNamespace(task_id="old-task-id", status=TaskStatus.SUCCESS)
    patches, _, _, celery_task_log_repo = _patched(stale_semantic_logs=[stale_log])
    campaign_candidate = _campaign_candidate()

    result, mock_task = _run(campaign_candidate, patches)

    assert result is True
    celery_task_log_repo.delete_by_task_id.assert_called_once_with("old-task-id")
    mock_task.apply_async.assert_called_once()


def test_deletes_a_stuck_queued_semantic_score_log():
    stuck_log = SimpleNamespace(task_id="stuck-queued-id", status=TaskStatus.QUEUED)
    patches, _, _, celery_task_log_repo = _patched(stale_semantic_logs=[stuck_log])
    campaign_candidate = _campaign_candidate()

    result, mock_task = _run(campaign_candidate, patches)

    assert result is True
    celery_task_log_repo.delete_by_task_id.assert_called_once_with("stuck-queued-id")


def test_leaves_a_terminal_failure_semantic_score_log_untouched():
    """FAILURE/DEAD rows are already reused-and-redispatched by semantic scoring's own retry branch - no cleanup needed here."""
    failed_log = SimpleNamespace(task_id="failed-task-id", status=TaskStatus.FAILURE)
    patches, _, _, celery_task_log_repo = _patched(stale_semantic_logs=[failed_log])
    campaign_candidate = _campaign_candidate()

    result, mock_task = _run(campaign_candidate, patches)

    assert result is True
    celery_task_log_repo.delete_by_task_id.assert_not_called()


def test_no_semantic_score_logs_at_all_is_a_clean_no_op():
    patches, _, _, celery_task_log_repo = _patched(stale_semantic_logs=[])
    campaign_candidate = _campaign_candidate()

    result, mock_task = _run(campaign_candidate, patches)

    assert result is True
    celery_task_log_repo.delete_by_task_id.assert_not_called()


def test_deletes_a_stuck_queued_ai_evaluate_log():
    stuck_log = SimpleNamespace(task_id="stuck-ai-eval-id", status=TaskStatus.QUEUED)
    patches, _, _, celery_task_log_repo = _patched(stale_ai_evaluate_logs=[stuck_log])
    campaign_candidate = _campaign_candidate()

    result, mock_task = _run(campaign_candidate, patches)

    assert result is True
    celery_task_log_repo.delete_by_task_id.assert_called_once_with("stuck-ai-eval-id")


def test_deletes_a_running_ai_evaluate_log():
    running_log = SimpleNamespace(task_id="running-ai-eval-id", status=TaskStatus.RUNNING)
    patches, _, _, celery_task_log_repo = _patched(stale_ai_evaluate_logs=[running_log])
    campaign_candidate = _campaign_candidate()

    result, mock_task = _run(campaign_candidate, patches)

    assert result is True
    celery_task_log_repo.delete_by_task_id.assert_called_once_with("running-ai-eval-id")


def test_leaves_a_completed_ai_evaluate_log_untouched():
    """SUCCESS/FAILURE/DEAD rows never block _enqueue_ai_evaluation's own dedup check - no cleanup needed."""
    success_log = SimpleNamespace(task_id="old-success-ai-eval-id", status=TaskStatus.SUCCESS)
    patches, _, _, celery_task_log_repo = _patched(stale_ai_evaluate_logs=[success_log])
    campaign_candidate = _campaign_candidate()

    result, mock_task = _run(campaign_candidate, patches)

    assert result is True
    celery_task_log_repo.delete_by_task_id.assert_not_called()


def test_cleans_up_both_task_types_in_the_same_run():
    stale_semantic = SimpleNamespace(task_id="stale-semantic-id", status=TaskStatus.SUCCESS)
    stuck_ai_eval = SimpleNamespace(task_id="stuck-ai-eval-id", status=TaskStatus.QUEUED)
    patches, _, _, celery_task_log_repo = _patched(
        stale_semantic_logs=[stale_semantic], stale_ai_evaluate_logs=[stuck_ai_eval],
    )
    campaign_candidate = _campaign_candidate()

    result, mock_task = _run(campaign_candidate, patches)

    assert result is True
    assert celery_task_log_repo.delete_by_task_id.call_count == 2
    celery_task_log_repo.delete_by_task_id.assert_any_call("stale-semantic-id")
    celery_task_log_repo.delete_by_task_id.assert_any_call("stuck-ai-eval-id")
