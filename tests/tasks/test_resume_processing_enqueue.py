from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

from app.services.celery_task_log_service import CeleryTaskLogService
from app.tasks.resume_processing_tasks import _enqueue_deterministic_scoring

MODULE = "app.tasks.resume_processing_tasks"


def _make_task_log_service():
    repo = MagicMock()
    repo.get_by_idempotency_key.return_value = None
    repo.create.side_effect = lambda log: log  # CeleryTaskLogRepository.create returns the same row
    repo.update.side_effect = lambda log: log
    return CeleryTaskLogService(repo), repo


def test_enqueues_once_per_campaign_candidate_for_the_resume():
    resume_id = uuid4()
    cc_a = SimpleNamespace(id=uuid4())
    cc_b = SimpleNamespace(id=uuid4())

    campaign_candidate_repo = MagicMock()
    campaign_candidate_repo.get_by_resume_id.return_value = [cc_a, cc_b]
    task_log_service, task_log_repo = _make_task_log_service()

    with patch(f"{MODULE}.CampaignCandidateRepository", return_value=campaign_candidate_repo), \
         patch(f"{MODULE}.calculate_deterministic_score_task") as mock_task:
        _enqueue_deterministic_scoring(MagicMock(), resume_id, task_log_service)

    assert mock_task.apply_async.call_count == 2
    enqueued_ids = {c.kwargs["kwargs"]["campaign_candidate_id"] for c in mock_task.apply_async.call_args_list}
    assert enqueued_ids == {str(cc_a.id), str(cc_b.id)}


def test_skips_enqueue_when_idempotency_key_already_logged():
    resume_id = uuid4()
    cc = SimpleNamespace(id=uuid4())

    campaign_candidate_repo = MagicMock()
    campaign_candidate_repo.get_by_resume_id.return_value = [cc]
    task_log_service, task_log_repo = _make_task_log_service()
    task_log_repo.get_by_idempotency_key.return_value = SimpleNamespace(id=uuid4())  # already logged

    with patch(f"{MODULE}.CampaignCandidateRepository", return_value=campaign_candidate_repo), \
         patch(f"{MODULE}.calculate_deterministic_score_task") as mock_task:
        _enqueue_deterministic_scoring(MagicMock(), resume_id, task_log_service)

    mock_task.apply_async.assert_not_called()
    task_log_repo.create.assert_not_called()


def test_broker_failure_enqueueing_is_logged_not_raised():
    resume_id = uuid4()
    cc = SimpleNamespace(id=uuid4())

    campaign_candidate_repo = MagicMock()
    campaign_candidate_repo.get_by_resume_id.return_value = [cc]
    task_log_service, task_log_repo = _make_task_log_service()

    with patch(f"{MODULE}.CampaignCandidateRepository", return_value=campaign_candidate_repo), \
         patch(f"{MODULE}.calculate_deterministic_score_task") as mock_task:
        mock_task.apply_async.side_effect = Exception("broker down")
        _enqueue_deterministic_scoring(MagicMock(), resume_id, task_log_service)  # must not raise

    assert mock_task.apply_async.called


def test_no_campaign_candidates_for_resume_enqueues_nothing():
    campaign_candidate_repo = MagicMock()
    campaign_candidate_repo.get_by_resume_id.return_value = []
    task_log_service, task_log_repo = _make_task_log_service()

    with patch(f"{MODULE}.CampaignCandidateRepository", return_value=campaign_candidate_repo), \
         patch(f"{MODULE}.calculate_deterministic_score_task") as mock_task:
        _enqueue_deterministic_scoring(MagicMock(), uuid4(), task_log_service)

    mock_task.apply_async.assert_not_called()
