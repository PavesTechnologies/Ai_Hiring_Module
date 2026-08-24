from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

from app.models.async_tasks import TaskStatus
from app.models.pipeline import AIEvaluationStatus
from app.tasks.deterministic_scoring_tasks import _cancel_downstream_ai_evaluation

"""
Bug fix - _cancel_downstream_ai_evaluation (called on both a DETERMINISTIC
and a SEMANTIC rejection) previously returned early, with no SKIPPED write
at all, whenever no QUEUED AI_EVALUATE task existed to cancel. That's the
common case: AI_EVALUATE is only ever queued after a semantic PASS, so a
candidate rejected on its first pass through deterministic or semantic
never had anything to cancel, and its ai_evaluation_status was left at
whatever get_or_create's default is (PENDING) - identical in shape to
"hasn't been processed yet" on the AI evaluation tab. Standalone test file
(isolated from the rest of tests/tasks/test_deterministic_scoring_tasks.py,
which has unrelated pre-existing collection/fixture breakage) since this
function is small and self-contained enough to test directly.
"""


def _cc():
    return SimpleNamespace(id=uuid4())


def test_marks_skipped_even_when_nothing_was_queued_to_cancel():
    campaign_candidate = _cc()
    task_log_repo = MagicMock()
    task_log_repo.get_by_campaign_candidate_and_task_type.return_value = []
    task_log_service = MagicMock()
    ai_evaluation = SimpleNamespace(ai_evaluation_status=AIEvaluationStatus.PENDING)
    ai_evaluation_repo = MagicMock()
    ai_evaluation_repo.get_or_create.return_value = ai_evaluation

    _cancel_downstream_ai_evaluation(campaign_candidate, task_log_repo, task_log_service, ai_evaluation_repo)

    ai_evaluation_repo.get_or_create.assert_called_once_with(campaign_candidate.id)
    assert ai_evaluation.ai_evaluation_status == AIEvaluationStatus.SKIPPED
    ai_evaluation_repo.update.assert_called_once_with(ai_evaluation)
    task_log_service.mark_dead.assert_not_called()


def test_cancels_a_queued_task_and_marks_skipped():
    campaign_candidate = _cc()
    queued_log = SimpleNamespace(task_id="queued-task-id", status=TaskStatus.QUEUED)
    task_log_repo = MagicMock()
    task_log_repo.get_by_campaign_candidate_and_task_type.return_value = [queued_log]
    task_log_service = MagicMock()
    ai_evaluation = SimpleNamespace(ai_evaluation_status=AIEvaluationStatus.PENDING)
    ai_evaluation_repo = MagicMock()
    ai_evaluation_repo.get_or_create.return_value = ai_evaluation

    _cancel_downstream_ai_evaluation(campaign_candidate, task_log_repo, task_log_service, ai_evaluation_repo)

    task_log_service.mark_dead.assert_called_once()
    assert task_log_service.mark_dead.call_args.args[0] is queued_log
    assert ai_evaluation.ai_evaluation_status == AIEvaluationStatus.SKIPPED


def test_ignores_non_queued_logs_but_still_marks_skipped():
    campaign_candidate = _cc()
    success_log = SimpleNamespace(task_id="old-success-id", status=TaskStatus.SUCCESS)
    task_log_repo = MagicMock()
    task_log_repo.get_by_campaign_candidate_and_task_type.return_value = [success_log]
    task_log_service = MagicMock()
    ai_evaluation = SimpleNamespace(ai_evaluation_status=AIEvaluationStatus.PENDING)
    ai_evaluation_repo = MagicMock()
    ai_evaluation_repo.get_or_create.return_value = ai_evaluation

    _cancel_downstream_ai_evaluation(campaign_candidate, task_log_repo, task_log_service, ai_evaluation_repo)

    task_log_service.mark_dead.assert_not_called()
    assert ai_evaluation.ai_evaluation_status == AIEvaluationStatus.SKIPPED
