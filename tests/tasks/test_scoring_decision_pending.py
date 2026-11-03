"""
Phase A of retiring v1: a v2 decision that can't be made never rejects.

- PENDING (provider failure) and NOT_RUN (daily LLM call cap) on a v2-deciding campaign roll back
  and retry the whole scoring with backoff; after the last attempt a replayable dead-letter row.
- The hourly recovery job re-queues candidates whose decision never landed - never one already in
  flight, and each at most once per SCORING_DECISION_RECOVERY_HOURS.
"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from app.models.async_tasks import TaskStatus
from app.models.pipeline import PipelineStage
from app.services.campaign.candidate_scoring_service import MandatorySkillMatchType
from app.services.document_processing.retry_policy import RetryPolicy
from tests.tasks.test_deterministic_scoring_tasks import (
    TASKS_MODULE, _breakdown, _Harness, _make_campaign, _make_campaign_candidate, _make_resume, _skill_entry,
)


class _Retry(Exception):
    pass


def _v2(outcome):
    return {"outcome": outcome, "outcome_reason": "daily LLM call cap reached" if outcome == "NOT_RUN" else "LLM down",
            "deterministic_passed": None, "deterministic_score": None}


def _run(h, outcome, *, last_attempt=False):
    from app.tasks.deterministic_scoring_tasks import calculate_deterministic_score_task
    campaign = _make_campaign()
    cc = _make_campaign_candidate(campaign.id, uuid4())
    h.campaign_candidate_repo.get_by_id.return_value = cc
    h.campaign_repo.get_by_id.return_value = campaign
    h.resume_repo.get_by_id.return_value = _make_resume()
    # v1 would reject this candidate - irrelevant: v2 decides and couldn't.
    h.scoring_service_instance.calculate_and_store_score_breakdown.return_value = _breakdown(
        [_skill_entry(uuid4(), MandatorySkillMatchType.MISSING.value)], 0.0, False)
    retry = MagicMock(side_effect=_Retry())
    dead_letters = MagicMock()
    policy = RetryPolicy(max_attempts=1 if last_attempt else 4, base_delay_seconds=120, max_delay_seconds=1800)
    with patch(f"{TASKS_MODULE}._run_scoring_v2", return_value=_v2(outcome)), \
            patch.object(calculate_deterministic_score_task, "retry", retry), \
            patch(f"{TASKS_MODULE}.DeadLetterQueueRepository", return_value=dead_letters), \
            patch(f"{TASKS_MODULE}._DECISION_PENDING_RETRY_POLICY", policy):
        if last_attempt:
            calculate_deterministic_score_task(campaign_candidate_id=str(cc.id))
        else:
            with pytest.raises(_Retry):
                calculate_deterministic_score_task(campaign_candidate_id=str(cc.id))
    return cc, retry, dead_letters


@pytest.mark.parametrize("outcome", ["NOT_RUN", "PENDING"])
def test_an_undecided_v2_result_retries_and_never_rejects(outcome):
    with _Harness() as h:
        cc, retry, dead_letters = _run(h, outcome)
        assert cc.pipeline_stage != PipelineStage.REJECTED
        h.queue_rejection_email_mock.assert_not_called()
        h.enqueue_semantic_scoring_mock.assert_not_called()
        assert retry.call_args.kwargs["countdown"] > 0 and retry.call_args.kwargs["max_retries"] == 4
        assert outcome in str(retry.call_args.kwargs["exc"])
        dead_letters.create.assert_not_called()
        assert h.task_log_repo.update.call_args.args[0].status == TaskStatus.RETRY


@pytest.mark.parametrize("outcome", ["NOT_RUN", "PENDING"])
def test_after_the_last_attempt_it_is_dead_lettered_and_replayable(outcome):
    with _Harness() as h:
        cc, retry, dead_letters = _run(h, outcome, last_attempt=True)
        retry.assert_not_called()
        assert cc.pipeline_stage != PipelineStage.REJECTED
        entry = dead_letters.create.call_args.kwargs
        assert entry["task_type"] == "DETERMINISTIC_SCORE" and entry["campaign_candidate_id"] == cc.id
        assert entry["input_payload"] == {"campaign_candidate_id": str(cc.id), "score_only": False}
        assert outcome in entry["final_error_message"]
        dead_letters.commit.assert_called_once()
        assert h.task_log_repo.update.call_args.args[0].status == TaskStatus.DEAD


def test_the_dead_letter_row_replays_through_the_existing_builder():
    from app.services.campaign.campaign_service import CampaignService
    from app.tasks.deterministic_scoring_tasks import calculate_deterministic_score_task
    entry = SimpleNamespace(campaign_candidate_id=uuid4())
    task_fn, kwargs, linkable = CampaignService._DLQ_REPLAY_BUILDERS["DETERMINISTIC_SCORE"](entry)
    assert task_fn is calculate_deterministic_score_task and linkable
    assert kwargs == {"campaign_candidate_id": str(entry.campaign_candidate_id)}


# --- recovery --------------------------------------------------------------------------------

NOW = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)


def _log(status, hours_ago, *, completed=True):
    at = NOW - timedelta(hours=hours_ago)
    return SimpleNamespace(status=status, queued_at=at, started_at=at, completed_at=at if completed else None)


@pytest.mark.parametrize("logs, expected", [
    ([], "requeue"),                                                     # never queued at all
    ([_log(TaskStatus.DEAD, 7)], "requeue"),                             # dead-lettered 7 h ago
    ([_log(TaskStatus.DEAD, 2)], "recent"),                              # ... only 2 h ago
    ([_log(TaskStatus.DEAD, 9), _log(TaskStatus.FAILURE, 1)], "recent"),  # the latest attempt counts
    ([_log(TaskStatus.DEAD, 9), _log(TaskStatus.RETRY, 0.2, completed=False)], "in_flight"),
    ([_log(TaskStatus.QUEUED, 30, completed=False)], "in_flight"),       # queued is never doubled
])
def test_recovery_action(logs, expected):
    from app.tasks.deterministic_scoring_tasks import recovery_action
    assert recovery_action(logs, NOW, 6) == expected


def test_the_recovery_job_requeues_only_what_is_due():
    from app.tasks import deterministic_scoring_tasks as tasks
    due, flying, recent = (SimpleNamespace(id=uuid4()) for _ in range(3))
    now = datetime.now(timezone.utc)
    logs = {due.id: [SimpleNamespace(status=TaskStatus.DEAD, queued_at=now - timedelta(hours=8),
                                     started_at=None, completed_at=now - timedelta(hours=7))],
            flying.id: [SimpleNamespace(status=TaskStatus.RETRY, queued_at=now, started_at=now, completed_at=None)],
            recent.id: [SimpleNamespace(status=TaskStatus.DEAD, queued_at=now, started_at=now, completed_at=now)]}
    candidates = MagicMock()
    candidates.get_never_screened.return_value = [due, flying, recent]
    task_logs = MagicMock()
    task_logs.get_by_campaign_candidate_and_task_type.side_effect = lambda cc_id, task_type: logs[cc_id]
    task_logs.get_by_task_id.return_value = None
    config = MagicMock()
    config.get_configs_by_keys.return_value = {}
    apply_async = MagicMock()
    with patch(f"{TASKS_MODULE}.SessionLocal", return_value=MagicMock()), \
            patch(f"{TASKS_MODULE}.CeleryTaskLogRepository", return_value=task_logs), \
            patch(f"{TASKS_MODULE}.CampaignCandidateRepository", return_value=candidates), \
            patch(f"{TASKS_MODULE}.ConfigRepository", return_value=config), \
            patch.object(tasks.calculate_deterministic_score_task, "apply_async", apply_async):
        tasks.recover_undecided_candidates()

    apply_async.assert_called_once()
    assert apply_async.call_args.kwargs["kwargs"] == {"campaign_candidate_id": str(due.id)}
    created = [c.args[0] for c in task_logs.create.call_args_list]
    queued = [log for log in created if log.task_type == "DETERMINISTIC_SCORE"]
    assert len(queued) == 1 and queued[0].campaign_candidate_id == due.id
    assert queued[0].task_id == apply_async.call_args.kwargs["task_id"]   # the log exists before dispatch
    statuses, cutoff = candidates.get_never_screened.call_args.args[0], \
        candidates.get_never_screened.call_args.kwargs["created_before"]
    assert {s.value for s in statuses} == {"ACTIVE", "PAUSED"} and cutoff < now - timedelta(minutes=29)
    summary = task_logs.update.call_args.args[0].output_summary
    assert '"requeued": 1' in summary and '"in_flight": 1' in summary and '"recent": 1' in summary


def test_the_recovery_job_is_scheduled_hourly():
    from app.core.celery_app import celery_app
    entry = celery_app.conf.beat_schedule["recover-undecided-candidates"]
    assert entry["task"] == "scoring.recover_undecided_candidates"
