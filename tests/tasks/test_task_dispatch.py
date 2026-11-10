"""
Review item 8: a failed hand-off to the queue no longer strands a candidate. The row is marked lost,
in-flight checks ignore rows older than any task is held, and the 5-minute sweep marks stalled rows of
every task type lost once no worker holds them.
"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql

from app.models.async_tasks import TaskStatus
from app.core.task_dispatch import (
    IN_FLIGHT_STALE_AFTER, dispatch_or_mark_lost, is_in_flight, is_lost, reset_for_redispatch,
)

NOW = datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc)


def _log(status, minutes_ago, *, started=True, **extra):
    at = NOW - timedelta(minutes=minutes_ago)
    return SimpleNamespace(status=status, queued_at=at, started_at=at if started else None, task_id=str(uuid4()),
                           task_type="X", dispatch_failed=False, error_message=None, completed_at=None, **extra)


@pytest.mark.parametrize("status, minutes_ago, expected", [
    (TaskStatus.QUEUED, 1, True),
    (TaskStatus.RUNNING, 30, True),
    (TaskStatus.RETRY, 89, True),
    (TaskStatus.QUEUED, 91, False),        # its message is gone (past the visibility timeout + 30 min)
    (TaskStatus.RUNNING, 600, False),
    (TaskStatus.FAILURE, 1, False),
    (TaskStatus.SUCCESS, 1, False),
])
def test_in_flight_has_an_age_limit(status, minutes_ago, expected):
    assert is_in_flight(_log(status, minutes_ago), NOW) is expected


def test_the_age_limit_outlasts_the_longest_hold():
    """Longest retry countdown today (480 s, the decision-pending and v2 policies) plus a long run."""
    from app.services.document_processing.retry_policy import compute_backoff_seconds
    from app.tasks.deterministic_scoring_tasks import _DECISION_PENDING_RETRY_POLICY as decision
    from app.tasks.scoring_v2_tasks import _SCORING_V2_RETRY_POLICY as v2
    longest = max(compute_backoff_seconds(p, n) for p in (decision, v2) for n in range(1, p.max_attempts))
    assert timedelta(seconds=longest) + timedelta(minutes=15) < IN_FLIGHT_STALE_AFTER


def test_a_failed_hand_off_marks_the_row_lost():
    log = _log(TaskStatus.QUEUED, 0)
    service = MagicMock()
    task = MagicMock()
    task.apply_async.side_effect = ConnectionError("broker unreachable")
    assert dispatch_or_mark_lost(task, kwargs={"a": 1}, log=log, task_log_service=service) is False
    assert log.status == TaskStatus.FAILURE and log.dispatch_failed is True and is_lost(log)
    assert "ConnectionError" in log.error_message and "broker unreachable" not in log.error_message
    service.repository.commit.assert_called_once()


def test_a_successful_hand_off_uses_the_rows_task_id():
    log = _log(TaskStatus.QUEUED, 0)
    task = MagicMock()
    assert dispatch_or_mark_lost(task, kwargs={"a": 1}, log=log, task_log_service=MagicMock()) is True
    task.apply_async.assert_called_once_with(kwargs={"a": 1}, task_id=log.task_id)
    assert log.status == TaskStatus.QUEUED


def test_marking_lost_never_raises():
    log = _log(TaskStatus.QUEUED, 0)
    service = MagicMock()
    service.repository.commit.side_effect = RuntimeError("db gone")
    task = MagicMock()
    task.apply_async.side_effect = ConnectionError()
    assert dispatch_or_mark_lost(task, kwargs={}, log=log, task_log_service=service) is False
    service.repository.rollback.assert_called_once()


def test_a_plain_failure_is_not_lost():
    assert not is_lost(_log(TaskStatus.FAILURE, 1))
    assert is_lost(SimpleNamespace(status=TaskStatus.FAILURE, dispatch_failed=True))
    assert not is_lost(SimpleNamespace(status=TaskStatus.QUEUED, dispatch_failed=True))   # resume: awaiting redispatch


def test_reset_for_redispatch():
    log = _log(TaskStatus.FAILURE, 60)
    log.dispatch_failed, log.error_message, log.completed_at = True, "lost", NOW
    reset_for_redispatch(log, "new-id")
    assert (log.task_id, log.status, log.dispatch_failed, log.error_message, log.started_at, log.completed_at) == \
        ("new-id", TaskStatus.QUEUED, False, None, None, None)


# -- the enqueue helpers ---------------------------------------------------------------------------

def _service_with(logs):
    service = MagicMock()
    service.repository.get_by_campaign_candidate_and_task_type.return_value = logs
    service.create_log.side_effect = lambda **kw: SimpleNamespace(**kw)
    return service


HELPERS = {
    "ai": ("app.tasks.ai_evaluation_tasks", "calculate_ai_evaluation_task",
           lambda mod, svc: mod._enqueue_ai_evaluation(SimpleNamespace(id=uuid4()), svc)),
    "composite": ("app.tasks.composite_scoring_tasks", "calculate_composite_score_task",
                  lambda mod, svc: mod._enqueue_composite_scoring(uuid4(), svc,
                                                                  list(mod.CompositeScoreTriggerSource)[0])),
    "v2": ("app.tasks.scoring_v2_tasks", "calculate_scoring_v2_task",
           lambda mod, svc: mod.enqueue_scoring_v2(SimpleNamespace(id=uuid4()), svc)),
}


@pytest.mark.parametrize("helper", sorted(HELPERS))
@pytest.mark.parametrize("minutes_ago, requeued", [(5, False), (120, True)])
def test_a_stale_row_no_longer_blocks_a_requeue(helper, minutes_ago, requeued):
    import importlib
    module_name, task_name, call = HELPERS[helper]
    mod = importlib.import_module(module_name)
    at = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    service = _service_with([SimpleNamespace(status=TaskStatus.QUEUED, queued_at=at, started_at=None)])
    with patch.object(getattr(mod, task_name), "apply_async") as apply_async:
        call(mod, service)
    assert apply_async.called is requeued


@pytest.mark.parametrize("helper", sorted(HELPERS))
def test_a_failed_hand_off_in_each_helper_marks_the_row_lost(helper):
    import importlib
    module_name, task_name, call = HELPERS[helper]
    mod = importlib.import_module(module_name)
    service = _service_with([])
    with patch.object(getattr(mod, task_name), "apply_async", side_effect=ConnectionError("down")):
        call(mod, service)
    created = service.create_log.call_args.kwargs["task_id"]
    stored = service.repository.update.call_args.args[0]
    assert stored.task_id == created and stored.status == TaskStatus.FAILURE and stored.dispatch_failed is True


@pytest.mark.parametrize("existing, reused", [
    (SimpleNamespace(status=TaskStatus.FAILURE, dispatch_failed=True), True),           # lost
    (SimpleNamespace(status=TaskStatus.QUEUED, dispatch_failed=False,
                     queued_at=datetime.now(timezone.utc) - timedelta(hours=2), started_at=None), True),   # stale
    (SimpleNamespace(status=TaskStatus.QUEUED, dispatch_failed=False,
                     queued_at=datetime.now(timezone.utc), started_at=None), False),     # in flight
    (SimpleNamespace(status=TaskStatus.FAILURE, dispatch_failed=False), False),         # a real failure
    (SimpleNamespace(status=TaskStatus.SUCCESS, dispatch_failed=False), False),
])
def test_the_resume_embedding_row_is_reused_only_when_lost_or_stale(existing, reused):
    from app.tasks import embedding_tasks as mod
    existing.task_id = "old"
    existing.task_type = "EMBED_RESUME"
    repo = MagicMock()
    repo.get_by_idempotency_key.return_value = existing
    service = MagicMock(repository=repo)
    with patch.object(mod.generate_resume_embedding_task, "apply_async") as apply_async:
        mod._enqueue_resume_embedding(MagicMock(), uuid4(), service)
    assert apply_async.called is reused
    repo.create_if_new_idempotency_key.assert_not_called()
    if reused:
        assert existing.task_id != "old" and apply_async.call_args.kwargs["task_id"] == existing.task_id


def test_a_stale_semantic_row_is_reused():
    from app.tasks import semantic_scoring_tasks as mod
    stale = SimpleNamespace(status=TaskStatus.RUNNING, task_id="old", task_type="SEMANTIC_SCORE",
                            queued_at=datetime.now(timezone.utc) - timedelta(hours=3), started_at=None)
    repo = MagicMock()
    repo.get_by_idempotency_key.return_value = stale
    repo.update.side_effect = lambda log: log
    resume_repo = MagicMock()
    resume_repo.get_embedding.return_value = SimpleNamespace(id=uuid4())
    with patch.object(mod.calculate_semantic_score_task, "apply_async") as apply_async:
        mod._enqueue_semantic_scoring(SimpleNamespace(id=uuid4(), resume_id=uuid4()), MagicMock(repository=repo),
                                      resume_repo)
    assert apply_async.called and stale.status == TaskStatus.QUEUED and stale.task_id != "old"


# -- the sweep -------------------------------------------------------------------------------------

def test_the_sweep_marks_stalled_rows_of_other_task_types_lost():
    from app.tasks import resume_processing_tasks as mod
    live, dead = SimpleNamespace(id=uuid4(), task_id="live", task_type="AI_EVALUATE", status=TaskStatus.RUNNING), \
        SimpleNamespace(id=uuid4(), task_id="dead", task_type="AI_EVALUATE", status=TaskStatus.QUEUED)
    repo = MagicMock()
    repo.get_stale_in_flight.return_value = [live, dead]
    repo.mark_lost_if_still_stale.return_value = True
    assert mod._mark_stalled_tasks_lost(repo, {"live"}) == 1
    assert repo.mark_lost_if_still_stale.call_args.args[0] == dead.id
    assert repo.get_stale_in_flight.call_args.kwargs["exclude_task_types"] == [mod.RESUME_DOCUMENT_PROCESSING_TASK_TYPE]


def test_the_sweep_does_nothing_when_the_workers_cannot_be_asked():
    from app.tasks import resume_processing_tasks as mod
    repo = MagicMock()
    assert mod._mark_stalled_tasks_lost(repo, None) == 0
    repo.get_stale_in_flight.assert_not_called()


def test_the_sweep_queries():
    from app.repositories.celery_task_log_repository import CeleryTaskLogRepository
    db = MagicMock()
    repo = CeleryTaskLogRepository(db)
    cutoff = NOW
    repo.mark_lost_if_still_stale(uuid4(), cutoff, "lost")
    sql = str(db.execute.call_args.args[0].compile(dialect=postgresql.dialect())).lower()
    assert "dispatch_failed" in sql and "coalesce(celery_task_log.started_at, celery_task_log.queued_at) <" in sql
    assert "status in" in sql
    db.commit.assert_called_once()
