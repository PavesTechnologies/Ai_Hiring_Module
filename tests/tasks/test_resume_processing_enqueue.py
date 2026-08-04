from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

from app.models.async_tasks import TaskStatus
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


# ----------------------------------------------------------------------
# Resume-upload resilience: recover_stalled_resume_uploads redispatches
# every RESUME_DOCUMENT_PROCESSING row whose apply_async failed at
# enqueue time (dispatch_failed=True), and only those rows.
# ----------------------------------------------------------------------

def _make_stalled_log(resume_id=None, task_id=None):
    return SimpleNamespace(id=uuid4(), task_id=task_id or str(uuid4()), resume_id=resume_id or uuid4())


def test_recovers_and_redispatches_every_claimed_stalled_task():
    from app.tasks.resume_processing_tasks import recover_stalled_resume_uploads

    stalled = [_make_stalled_log(), _make_stalled_log()]
    task_log_repo = MagicMock()
    task_log_repo.get_queued_dispatch_failed.return_value = stalled
    task_log_repo.claim_for_redispatch.return_value = True

    campaign_candidate_repo = MagicMock()
    campaign_candidate_repo.get_by_resume_id.return_value = [SimpleNamespace(campaign_id=uuid4())]
    campaign_repo = MagicMock()
    campaign_repo.get_by_id.return_value = SimpleNamespace(prompt_template_id=uuid4())

    with patch(f"{MODULE}.CeleryTaskLogRepository", return_value=task_log_repo), \
         patch(f"{MODULE}.CampaignCandidateRepository", return_value=campaign_candidate_repo), \
         patch(f"{MODULE}.CampaignRepository", return_value=campaign_repo), \
         patch(f"{MODULE}.process_resume_document") as mock_task:
        recovered_count = recover_stalled_resume_uploads(MagicMock())

    assert recovered_count == 2
    assert mock_task.apply_async.call_count == 2
    dispatched_task_ids = {c.kwargs["task_id"] for c in mock_task.apply_async.call_args_list}
    assert dispatched_task_ids == {log.task_id for log in stalled}


def test_skips_rows_that_lose_the_claim_race():
    from app.tasks.resume_processing_tasks import recover_stalled_resume_uploads

    task_log_repo = MagicMock()
    task_log_repo.get_queued_dispatch_failed.return_value = [_make_stalled_log()]
    task_log_repo.claim_for_redispatch.return_value = False  # another recovery run already claimed it

    with patch(f"{MODULE}.CeleryTaskLogRepository", return_value=task_log_repo), \
         patch(f"{MODULE}.CampaignCandidateRepository"), \
         patch(f"{MODULE}.CampaignRepository"), \
         patch(f"{MODULE}.process_resume_document") as mock_task:
        recovered_count = recover_stalled_resume_uploads(MagicMock())

    assert recovered_count == 0
    mock_task.apply_async.assert_not_called()


def test_redispatch_failure_is_logged_and_row_marked_dispatch_failed_again():
    from app.tasks.resume_processing_tasks import recover_stalled_resume_uploads

    stalled_log = _make_stalled_log()
    task_log_repo = MagicMock()
    task_log_repo.get_queued_dispatch_failed.return_value = [stalled_log]
    task_log_repo.claim_for_redispatch.return_value = True
    task_log_repo.update.side_effect = lambda log: log

    campaign_candidate_repo = MagicMock()
    campaign_candidate_repo.get_by_resume_id.return_value = [SimpleNamespace(campaign_id=uuid4())]
    campaign_repo = MagicMock()
    campaign_repo.get_by_id.return_value = SimpleNamespace(prompt_template_id=uuid4())

    with patch(f"{MODULE}.CeleryTaskLogRepository", return_value=task_log_repo), \
         patch(f"{MODULE}.CampaignCandidateRepository", return_value=campaign_candidate_repo), \
         patch(f"{MODULE}.CampaignRepository", return_value=campaign_repo), \
         patch(f"{MODULE}.process_resume_document") as mock_task:
        mock_task.apply_async.side_effect = Exception("broker still down")

        # Must not raise.
        recovered_count = recover_stalled_resume_uploads(MagicMock())

    assert recovered_count == 0
    updated_log = task_log_repo.update.call_args.args[0]
    assert updated_log.dispatch_failed is True


def test_missing_campaign_marks_dispatch_failed_without_crashing():
    from app.tasks.resume_processing_tasks import recover_stalled_resume_uploads

    stalled_log = _make_stalled_log()
    task_log_repo = MagicMock()
    task_log_repo.get_queued_dispatch_failed.return_value = [stalled_log]
    task_log_repo.claim_for_redispatch.return_value = True
    task_log_repo.update.side_effect = lambda log: log

    campaign_candidate_repo = MagicMock()
    campaign_candidate_repo.get_by_resume_id.return_value = []  # orphaned resume_id

    with patch(f"{MODULE}.CeleryTaskLogRepository", return_value=task_log_repo), \
         patch(f"{MODULE}.CampaignCandidateRepository", return_value=campaign_candidate_repo), \
         patch(f"{MODULE}.CampaignRepository"), \
         patch(f"{MODULE}.process_resume_document") as mock_task:
        recovered_count = recover_stalled_resume_uploads(MagicMock())

    assert recovered_count == 0
    mock_task.apply_async.assert_not_called()


def test_recovery_task_wrapper_creates_its_own_task_log_and_marks_success():
    from app.tasks.resume_processing_tasks import recover_stalled_resume_uploads_task

    task_log_repo = MagicMock()

    def _create(log):
        log.retry_count = getattr(log, "retry_count", 0) or 0
        return log
    task_log_repo.create.side_effect = _create
    task_log_repo.update.side_effect = lambda log: log

    with patch(f"{MODULE}.SessionLocal", return_value=MagicMock()), \
         patch(f"{MODULE}.CeleryTaskLogRepository", return_value=task_log_repo), \
         patch(f"{MODULE}.recover_stalled_resume_uploads", return_value=3) as mock_recover:
        recover_stalled_resume_uploads_task()

    mock_recover.assert_called_once()
    task_log = task_log_repo.update.call_args.args[0]
    assert task_log.status == TaskStatus.SUCCESS
    assert "3" in task_log.output_summary
