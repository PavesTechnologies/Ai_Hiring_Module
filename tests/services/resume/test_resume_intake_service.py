from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import UUID, uuid4

from app.models.async_tasks import TaskStatus
from app.models.campaigns import CampaignStatus
from app.models.candidates import FileFormat
from app.services.resume.resume_intake_service import ResumeIntakeService

MODULE = "app.services.resume.resume_intake_service"


def _make_campaign(campaign_id=None):
    return SimpleNamespace(
        id=campaign_id or uuid4(), status=CampaignStatus.ACTIVE, max_candidates=None,
        prompt_template_id=uuid4(), name="Backend Engineer",
    )


def _make_resume(resume_id=None, candidate_id=None):
    return SimpleNamespace(
        id=resume_id or uuid4(), candidate_id=candidate_id or uuid4(),
        file_format=FileFormat.PDF, task_id=None,
    )


def _make_harness(requires_processing=True, race_existing_log=None):
    """Every real dependency mocked; only ResumeIntakeService.upload_resume itself runs for real."""
    campaign = _make_campaign()
    resume = _make_resume()

    resume_service = MagicMock()
    resume_service.upload.return_value = SimpleNamespace(resume=resume, requires_processing=requires_processing)

    campaign_candidate_service = MagicMock()
    campaign_candidate_service.create_campaign_candidate.return_value = SimpleNamespace(id=uuid4())

    campaign_repo = MagicMock()
    campaign_repo.get_by_id.return_value = campaign
    campaign_repo.get_candidate_count.return_value = 0

    audit_service = MagicMock()

    task_log_repo = MagicMock()
    if race_existing_log is not None:
        # Lost the race - another request already created the log for this resume.
        task_log_repo.create_if_new_idempotency_key.return_value = (race_existing_log, False)
    else:
        task_log_repo.create_if_new_idempotency_key.side_effect = lambda log: (log, True)

    service = ResumeIntakeService(
        resume_service=resume_service,
        campaign_candidate_service=campaign_candidate_service,
        campaign_repo=campaign_repo,
        audit_service=audit_service,
        task_log_repo=task_log_repo,
    )
    return service, campaign, resume, resume_service, campaign_repo, task_log_repo


def _upload(service, campaign):
    return service.upload_resume(
        campaign_id=campaign.id,
        file_bytes=b"pdf-bytes",
        filename="resume.pdf",
        candidate_full_name="Jane Doe",
        candidate_email="jane@example.com",
        jurisdiction="GLOBAL",
        uploaded_by="hr-1",
        actor_role="HR_ADMIN",
    )


# ----------------------------------------------------------------------
# Core requirement: the celery_task_log row is created and committed
# BEFORE apply_async is ever attempted - never a task_id without a
# matching row.
# ----------------------------------------------------------------------

def test_creates_and_commits_celery_task_log_before_apply_async():
    service, campaign, resume, *_ , task_log_repo = _make_harness()

    with patch(f"{MODULE}.process_resume_document") as mock_task:
        _resume, _cc, _campaign, task_id, requires_processing = _upload(service, campaign)

    assert requires_processing is True
    task_log_repo.create_if_new_idempotency_key.assert_called_once()
    created_log = task_log_repo.create_if_new_idempotency_key.call_args.args[0]
    assert created_log.task_type == "RESUME_DOCUMENT_PROCESSING"
    assert created_log.resume_id == resume.id
    assert created_log.status == TaskStatus.QUEUED
    assert created_log.idempotency_key == f"RESUME_DOCUMENT_PROCESSING:{resume.id}"
    task_log_repo.commit.assert_called_once()

    # And the dispatch itself happens with that same task_id.
    mock_task.apply_async.assert_called_once_with(
        kwargs={"resume_id": str(resume.id), "prompt_template_id": str(campaign.prompt_template_id)},
        task_id=str(task_id),
    )
    assert task_id is not None


# ----------------------------------------------------------------------
# Celery/Redis unavailable: upload must still succeed, resume/candidate
# data must never be rolled back, and the failure must be recorded
# without changing celery_task_log.status away from QUEUED.
# ----------------------------------------------------------------------

def test_apply_async_failure_never_fails_the_upload():
    service, campaign, resume, *_, task_log_repo = _make_harness()

    with patch(f"{MODULE}.process_resume_document") as mock_task:
        mock_task.apply_async.side_effect = Exception("Connection refused - broker unreachable")

        # Must not raise.
        result_resume, campaign_candidate, result_campaign, task_id, requires_processing = _upload(service, campaign)

    assert result_resume is resume
    assert requires_processing is True
    assert task_id is not None


def test_apply_async_failure_keeps_status_queued_and_sets_dispatch_failed():
    service, campaign, resume, *_, task_log_repo = _make_harness()

    with patch(f"{MODULE}.process_resume_document") as mock_task:
        mock_task.apply_async.side_effect = Exception("Connection refused - broker unreachable")
        _upload(service, campaign)

    # update() is called by mark_dispatch_failed via CeleryTaskLogService.
    updated_log = task_log_repo.update.call_args.args[0]
    assert updated_log.status == TaskStatus.QUEUED
    assert updated_log.dispatch_failed is True
    assert "broker unreachable" in updated_log.error_message
    assert "enqueue_failed" in updated_log.output_summary


def test_apply_async_failure_never_rolls_back_the_upload_transaction():
    service, campaign, resume, *_ = _make_harness()

    with patch(f"{MODULE}.process_resume_document") as mock_task:
        mock_task.apply_async.side_effect = Exception("broker down")
        _upload(service, campaign)

    service.campaign_repo.rollback.assert_not_called()


# ----------------------------------------------------------------------
# Idempotency: a concurrent request for the exact same resume_id must
# never dispatch twice - the loser reuses the winner's task_id.
# ----------------------------------------------------------------------

def test_lost_idempotency_race_reuses_existing_task_id_and_never_dispatches():
    existing_task_id = str(uuid4())
    existing_log = SimpleNamespace(task_id=existing_task_id)
    service, campaign, resume, *_ = _make_harness(race_existing_log=existing_log)

    with patch(f"{MODULE}.process_resume_document") as mock_task:
        _resume, _cc, _campaign, task_id, requires_processing = _upload(service, campaign)

    assert requires_processing is True
    assert task_id == UUID(existing_task_id)
    mock_task.apply_async.assert_not_called()


# ----------------------------------------------------------------------
# Existing "use_existing" (no reprocessing needed) behaviour must be
# fully preserved - no celery_task_log row, no Celery dispatch at all.
# ----------------------------------------------------------------------

def test_use_existing_resolution_never_touches_celery_task_log():
    service, campaign, resume, *_, task_log_repo = _make_harness(requires_processing=False)
    resume.task_id = str(uuid4())

    with patch(f"{MODULE}.process_resume_document") as mock_task:
        _resume, _cc, _campaign, task_id, requires_processing = _upload(service, campaign)

    assert requires_processing is False
    assert task_id == UUID(resume.task_id)
    task_log_repo.create_if_new_idempotency_key.assert_not_called()
    mock_task.apply_async.assert_not_called()
