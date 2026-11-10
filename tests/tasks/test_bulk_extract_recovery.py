"""
Review item 23: bulk ZIP extraction recovers after a crash or a redelivery. The task log is got or
created (a redelivery no longer dies on the unique task_id), extraction resumes from what an earlier
run staged, and the 5-minute sweep restarts stalled extractions and re-dispatches lost file parses.
"""
import io
import zipfile
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from app.models.async_tasks import BulkUploadFileStatus, BulkUploadStatus, TaskStatus

MODULE = "app.tasks.bulk_upload_tasks"


def _zip(*names):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name in names:
            archive.writestr(name, b"%PDF fake")
    return buffer.getvalue()


@contextmanager
def _world(job_status=BulkUploadStatus.PENDING, staged=(), existing_log=None, existing_objects=()):
    job = SimpleNamespace(id=uuid4(), campaign_id=uuid4(), status=job_status, consent_confirmed=True,
                          zip_storage_path="z.zip", original_filename="cvs.zip", created_at=None)
    w = SimpleNamespace(job=job)
    w.job_repo = MagicMock()
    w.job_repo.get_by_id.return_value = job
    w.file_repo = MagicMock()
    w.file_repo.get_by_job_id.return_value = list(staged)
    w.file_repo.create_many.side_effect = lambda files: [SimpleNamespace(id=uuid4(), **{
        k: getattr(f, k) for k in ("task_id", "status", "storage_path", "original_filename")}) for f in files]
    w.log_repo = MagicMock()
    w.log_repo.get_by_task_id.side_effect = lambda task_id: existing_log if task_id == "extract-1" else None
    w.log_service = MagicMock()
    w.log_service.mark_running.side_effect = lambda log: log
    w.log_service.create_log.side_effect = lambda **kw: SimpleNamespace(**kw)
    w.storage = MagicMock()
    w.storage.download_file.return_value = _zip("a/CV.pdf", "b/CV.pdf")
    w.storage.file_exists.side_effect = lambda bucket, path: path in existing_objects
    w.campaign_repo = MagicMock()
    w.campaign_repo.get_by_id.return_value = SimpleNamespace(status=None)
    w.parse = MagicMock()
    with patch(f"{MODULE}.SessionLocal", return_value=MagicMock()), \
            patch(f"{MODULE}.BulkUploadJobRepository", return_value=w.job_repo), \
            patch(f"{MODULE}.BulkUploadJobFileRepository", return_value=w.file_repo), \
            patch(f"{MODULE}.ConfigRepository"), \
            patch(f"{MODULE}.CeleryTaskLogRepository", return_value=w.log_repo), \
            patch(f"{MODULE}.CeleryTaskLogService", return_value=w.log_service), \
            patch(f"{MODULE}.StorageService", return_value=w.storage), \
            patch(f"{MODULE}.ZipValidationService"), \
            patch(f"{MODULE}.CampaignRepository", return_value=w.campaign_repo), \
            patch(f"{MODULE}.parse_bulk_upload_file", w.parse):
        yield w


def _run(w):
    from app.tasks.bulk_upload_tasks import extract_bulk_upload_zip
    extract_bulk_upload_zip(task_id="extract-1", bulk_upload_job_id=str(w.job.id))


def _file(status=BulkUploadFileStatus.QUEUED):
    return SimpleNamespace(id=uuid4(), task_id=str(uuid4()), status=status)


def test_a_redelivered_extraction_reuses_its_log():
    log = SimpleNamespace(task_id="extract-1", status=TaskStatus.RUNNING)
    with _world(existing_log=log) as w:
        _run(w)
    w.log_service.create_log.assert_not_called()
    w.log_service.mark_running.assert_called_with(log)
    assert w.parse.apply_async.call_count == 2


def test_an_extraction_that_already_succeeded_does_nothing():
    with _world(existing_log=SimpleNamespace(task_id="extract-1", status=TaskStatus.SUCCESS)) as w:
        _run(w)
    w.job_repo.get_by_id.assert_not_called()
    w.parse.apply_async.assert_not_called()


def test_staged_object_paths_are_fixed_per_entry_and_reused():
    with _world() as w:
        prefix = f"campaign_{w.job.campaign_id}/bulk-zip/{w.job.id}/"
    with _world(existing_objects={prefix + "0000_CV.pdf"}) as w2:
        w2.job.id, w2.job.campaign_id = w.job.id, w.job.campaign_id
        _run(w2)
    uploaded = [c.kwargs["file_path"] for c in w2.storage.upload_file.call_args_list]
    assert uploaded == [prefix + "0001_CV.pdf"]       # the first was staged by an earlier run
    paths = [f.storage_path for f in w2.file_repo.create_many.call_args.args[0]]
    assert paths == [prefix + "0000_CV.pdf", prefix + "0001_CV.pdf"]   # same names, two candidates


def test_a_run_that_finds_its_files_recorded_does_not_extract_again():
    staged = [_file(), _file()]
    with _world(job_status=BulkUploadStatus.EXTRACTING, staged=staged) as w:
        _run(w)
    w.storage.download_file.assert_not_called()
    w.job_repo.set_total_files.assert_called_once_with(w.job.id, 2)
    assert w.job_repo.update_status.call_args.args[1] == BulkUploadStatus.PROCESSING
    assert w.parse.apply_async.call_count == 2


def test_a_redelivery_after_extraction_hands_over_only_undispatched_files():
    dispatched, waiting = _file(), _file()
    with _world(job_status=BulkUploadStatus.PROCESSING, staged=[dispatched, waiting]) as w:
        w.log_repo.get_by_task_id.side_effect = lambda task_id: (
            SimpleNamespace(status=TaskStatus.RUNNING, started_at=datetime.now(timezone.utc), queued_at=None)
            if task_id == dispatched.task_id else None)
        _run(w)
    w.storage.download_file.assert_not_called()
    w.job_repo.update_status.assert_not_called()
    assert [c.kwargs["task_id"] for c in w.parse.apply_async.call_args_list] == [waiting.task_id]


# -- dispatch_waiting_files ------------------------------------------------------------------------

LOST = SimpleNamespace(status=TaskStatus.FAILURE, dispatch_failed=True)
STALE = SimpleNamespace(status=TaskStatus.RUNNING, started_at=datetime.now(timezone.utc) - timedelta(hours=3),
                        queued_at=None, dispatch_failed=False)
LIVE = SimpleNamespace(status=TaskStatus.RUNNING, started_at=datetime.now(timezone.utc), queued_at=None,
                       dispatch_failed=False)


@pytest.mark.parametrize("status, log, live, due", [
    (BulkUploadFileStatus.QUEUED, None, None, True),               # never dispatched
    (BulkUploadFileStatus.QUEUED, LOST, None, False),              # without asking the workers: never
    (BulkUploadFileStatus.QUEUED, LOST, set(), True),
    (BulkUploadFileStatus.RUNNING, STALE, set(), True),            # stalled mid-parse, no worker holds it
    (BulkUploadFileStatus.RUNNING, STALE, "own", False),           # a worker still holds it
    (BulkUploadFileStatus.RUNNING, LIVE, set(), False),
    (BulkUploadFileStatus.PROCESSED, None, set(), False),
])
def test_which_files_are_dispatched(status, log, live, due):
    from app.tasks.bulk_upload_tasks import dispatch_waiting_files
    job_file = _file(status)
    repo = MagicMock()
    repo.get_by_task_id.return_value = log
    live_ids = {job_file.task_id} if live == "own" else live
    with patch(f"{MODULE}.parse_bulk_upload_file") as parse:
        assert dispatch_waiting_files(repo, [job_file], live_ids) == (1 if due else 0)
    if due:
        assert parse.apply_async.call_args.kwargs["task_id"] == job_file.task_id


# -- the sweep -------------------------------------------------------------------------------------

def _sweep(jobs, logs_by_job, live=frozenset()):
    from app.tasks import bulk_upload_tasks as mod
    job_repo, file_repo, log_repo = MagicMock(), MagicMock(), MagicMock()
    job_repo.get_by_statuses.return_value = jobs
    log_repo.get_by_bulk_upload_job_and_task_type.side_effect = lambda job_id, task_type: logs_by_job.get(job_id, [])
    with patch(f"{MODULE}.BulkUploadJobRepository", return_value=job_repo), \
            patch(f"{MODULE}.BulkUploadJobFileRepository", return_value=file_repo), \
            patch(f"{MODULE}.CeleryTaskLogRepository", return_value=log_repo), \
            patch.object(mod.extract_bulk_upload_zip, "apply_async") as extract, \
            patch(f"{MODULE}.dispatch_waiting_files", return_value=2) as dispatch:
        counts = mod.recover_stalled_bulk_jobs(MagicMock(), set(live) if live is not None else None)
    return counts, extract, dispatch


def _job(status, created_hours_ago=5):
    return SimpleNamespace(id=uuid4(), status=status,
                           created_at=datetime.now(timezone.utc) - timedelta(hours=created_hours_ago))


def _extract_log(status, hours_ago, task_id="x"):
    at = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    return SimpleNamespace(status=status, started_at=at, queued_at=at, completed_at=None, task_id=task_id)


def test_a_stalled_extraction_is_restarted_as_a_new_task():
    job = _job(BulkUploadStatus.EXTRACTING)
    counts, extract, _ = _sweep([job], {job.id: [_extract_log(TaskStatus.FAILURE, 3)]})
    assert counts["extractions_restarted"] == 1
    kwargs = extract.call_args.kwargs
    assert kwargs["kwargs"]["bulk_upload_job_id"] == str(job.id) and kwargs["task_id"] == kwargs["kwargs"]["task_id"]


def test_a_pending_job_whose_hand_off_failed_is_extracted():
    job = _job(BulkUploadStatus.PENDING)
    counts, extract, _ = _sweep([job], {})
    assert counts["extractions_restarted"] == 1


@pytest.mark.parametrize("logs, created_hours_ago", [
    ([_extract_log(TaskStatus.RUNNING, 0.1)], 5),     # still extracting
    ([], 0.5),                                        # just uploaded
    ([_extract_log(TaskStatus.FAILURE, 0.5)], 5),     # ended too recently
])
def test_recent_or_running_extractions_are_left_alone(logs, created_hours_ago):
    job = _job(BulkUploadStatus.EXTRACTING, created_hours_ago)
    counts, extract, _ = _sweep([job], {job.id: logs})
    extract.assert_not_called()


def test_a_worker_still_holding_the_extraction_is_left_alone():
    job = _job(BulkUploadStatus.EXTRACTING)
    counts, extract, _ = _sweep([job], {job.id: [_extract_log(TaskStatus.RUNNING, 3, task_id="held")]},
                                live={"held"})
    extract.assert_not_called()


def test_a_processing_job_has_its_waiting_files_dispatched_with_the_workers_answer():
    job = _job(BulkUploadStatus.PROCESSING)
    counts, extract, dispatch = _sweep([job], {job.id: [_extract_log(TaskStatus.SUCCESS, 3)]}, live={"z"})
    extract.assert_not_called()
    assert dispatch.call_args.args[2] == {"z"} and counts["files_dispatched"] == 2


def test_nothing_happens_when_the_workers_cannot_be_asked():
    job = _job(BulkUploadStatus.EXTRACTING)
    counts, extract, dispatch = _sweep([job], {}, live=None)
    extract.assert_not_called() and dispatch.assert_not_called()
    assert counts == {"extractions_restarted": 0, "files_dispatched": 0}
