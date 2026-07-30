import pytest
from unittest.mock import MagicMock

from app.exception_handler.exceptions import ConflictError, NotFoundError
from app.models.async_tasks import CeleryTaskLog, DeadLetterQueue, TaskStatus
from app.services.document_processing.dead_letter_cleanup_service import DeadLetterCleanupService


def _make_dlq_entry(task_type="JD_DOCUMENT_PROCESSING", input_payload=None):
    entry = MagicMock(spec=DeadLetterQueue)
    entry.original_task_id = "task-1"
    entry.task_type = task_type
    entry.input_payload = input_payload
    return entry


def _make_task_log(status=TaskStatus.FAILURE):
    task_log = MagicMock(spec=CeleryTaskLog)
    task_log.task_id = "task-1"
    task_log.status = status
    return task_log


def _make_service(dlq_entry=None, task_log=None):
    dead_letter_queue_repo = MagicMock()
    dead_letter_queue_repo.get_by_task_id.return_value = dlq_entry
    celery_task_log_repo = MagicMock()
    celery_task_log_repo.get_by_task_id.return_value = task_log
    checkpoint_repo = MagicMock()
    stage_failure_log_repo = MagicMock()
    document_processing_repo = MagicMock()
    storage_service = MagicMock()
    service = DeadLetterCleanupService(
        dead_letter_queue_repo=dead_letter_queue_repo,
        celery_task_log_repo=celery_task_log_repo,
        checkpoint_repo=checkpoint_repo,
        stage_failure_log_repo=stage_failure_log_repo,
        document_processing_repo=document_processing_repo,
        storage_service=storage_service,
    )
    return (
        service, dead_letter_queue_repo, celery_task_log_repo,
        checkpoint_repo, stage_failure_log_repo, document_processing_repo, storage_service,
    )


# ── Dead-lettered path (dead_letter_queue row exists) ───────────────────────

def test_purge_deletes_all_tracking_rows_and_commits():
    entry = _make_dlq_entry(input_payload={"file_path": "jd/some-file.pdf"})
    service, dlq_repo, task_log_repo, checkpoint_repo, failure_log_repo, stage_exec_repo, storage_service = _make_service(dlq_entry=entry)

    result = service.purge("task-1")

    assert result is entry
    failure_log_repo.delete_by_task_id.assert_called_once_with("task-1")
    stage_exec_repo.delete_by_task_id.assert_called_once_with("task-1")
    checkpoint_repo.delete.assert_called_once_with("task-1")
    dlq_repo.delete_by_task_id.assert_called_once_with("task-1")
    dlq_repo.commit.assert_called_once()
    task_log_repo.delete_by_task_id.assert_not_called()


def test_purge_deletes_jd_file_when_task_type_is_jd_with_file_path():
    entry = _make_dlq_entry(task_type="JD_DOCUMENT_PROCESSING", input_payload={"file_path": "jd/some-file.pdf"})
    service, *_rest, storage_service = _make_service(dlq_entry=entry)

    service.purge("task-1")

    storage_service.delete_file.assert_called_once_with(bucket_name="airs-job-descriptions", file_path="jd/some-file.pdf")


def test_purge_does_not_touch_storage_for_resume_task_type():
    entry = _make_dlq_entry(task_type="RESUME_DOCUMENT_PROCESSING", input_payload=None)
    service, *_rest, storage_service = _make_service(dlq_entry=entry)

    service.purge("task-1")

    storage_service.delete_file.assert_not_called()


def test_purge_skips_file_deletion_when_no_file_path_captured():
    entry = _make_dlq_entry(task_type="JD_DOCUMENT_PROCESSING", input_payload={})
    service, *_rest, storage_service = _make_service(dlq_entry=entry)

    service.purge("task-1")

    storage_service.delete_file.assert_not_called()


def test_purge_storage_failure_does_not_block_db_cleanup():
    entry = _make_dlq_entry(task_type="JD_DOCUMENT_PROCESSING", input_payload={"file_path": "jd/some-file.pdf"})
    service, dlq_repo, *_rest, storage_service = _make_service(dlq_entry=entry)
    storage_service.delete_file.side_effect = Exception("storage provider down")

    service.purge("task-1")

    dlq_repo.delete_by_task_id.assert_called_once_with("task-1")
    dlq_repo.commit.assert_called_once()


def test_purge_rolls_back_on_db_failure():
    entry = _make_dlq_entry()
    service, dlq_repo, _task_log_repo, _checkpoint_repo, _failure_log_repo, stage_exec_repo, _storage = _make_service(dlq_entry=entry)
    stage_exec_repo.delete_by_task_id.side_effect = ConnectionError("connection lost")

    with pytest.raises(ConnectionError):
        service.purge("task-1")

    dlq_repo.rollback.assert_called_once()
    dlq_repo.commit.assert_not_called()


# ── Orphaned-failure path (no dead_letter_queue row) ────────────────────────

def test_purge_falls_back_to_celery_task_log_when_no_dead_letter_entry():
    task_log = _make_task_log(status=TaskStatus.FAILURE)
    service, dlq_repo, task_log_repo, checkpoint_repo, failure_log_repo, stage_exec_repo, storage_service = _make_service(
        dlq_entry=None, task_log=task_log,
    )

    result = service.purge("task-1")

    assert result is task_log
    failure_log_repo.delete_by_task_id.assert_called_once_with("task-1")
    stage_exec_repo.delete_by_task_id.assert_called_once_with("task-1")
    checkpoint_repo.delete.assert_called_once_with("task-1")
    task_log_repo.delete_by_task_id.assert_called_once_with("task-1")
    task_log_repo.commit.assert_called_once()
    dlq_repo.delete_by_task_id.assert_not_called()
    storage_service.delete_file.assert_not_called()


def test_purge_raises_not_found_when_neither_dead_letter_nor_task_log_exists():
    service, *_rest = _make_service(dlq_entry=None, task_log=None)

    with pytest.raises(NotFoundError):
        service.purge("missing-task")


@pytest.mark.parametrize("status", [TaskStatus.QUEUED, TaskStatus.RUNNING, TaskStatus.SUCCESS, TaskStatus.RETRY])
def test_purge_refuses_non_failed_task(status):
    task_log = _make_task_log(status=status)
    service, dlq_repo, task_log_repo, *_rest = _make_service(dlq_entry=None, task_log=task_log)

    with pytest.raises(ConflictError):
        service.purge("task-1")

    task_log_repo.delete_by_task_id.assert_not_called()


def test_purge_orphaned_failure_rolls_back_on_db_failure():
    task_log = _make_task_log(status=TaskStatus.FAILURE)
    service, _dlq_repo, task_log_repo, _checkpoint_repo, _failure_log_repo, stage_exec_repo, _storage = _make_service(
        dlq_entry=None, task_log=task_log,
    )
    stage_exec_repo.delete_by_task_id.side_effect = ConnectionError("connection lost")

    with pytest.raises(ConnectionError):
        service.purge("task-1")

    task_log_repo.rollback.assert_called_once()
    task_log_repo.commit.assert_not_called()
