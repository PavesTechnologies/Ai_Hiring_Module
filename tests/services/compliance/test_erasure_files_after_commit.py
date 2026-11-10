"""
Review item 14: erasure commits the database deletion first and deletes the stored files after it; a
failed file delete is retried in the background, audited (counts only) and reported - never reported as
success. A rebuilt ZIP is removed again when the erasure rolls back. Repositories, storage and Celery are
mocked. No email.
"""
import io
import zipfile
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from app.services.compliance.candidate_erasure_service import CandidateErasureService


@pytest.fixture(autouse=True)
def no_email():
    with patch("app.services.notifications.ses_email_client.SESEmailClient.send_email",
               side_effect=AssertionError("email sent")):
        yield


def _zip(*names) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name in names:
            archive.writestr(f"batch/{name}", f"content of {name}")
    return buffer.getvalue()


def _service(storage=None, resumes=None):
    candidate = SimpleNamespace(id=uuid4(), encryption_key_id=uuid4(), full_name_encrypted=None,
                                email_encrypted=None, phone_encrypted=None)
    calls = MagicMock()
    calls.candidate.get_by_id.return_value = candidate
    calls.resume.get_all_versions_by_candidate.return_value = resumes if resumes is not None else [
        SimpleNamespace(id=uuid4(), file_path="org/resume/a.pdf", original_filename="a.pdf", bulk_upload_job_id=None),
        SimpleNamespace(id=uuid4(), file_path="org/resume/b.pdf", original_filename="b.pdf", bulk_upload_job_id=None)]
    calls.cc.get_by_candidate_id.return_value = []
    calls.task_log.find_task_ids_mentioning.return_value = []
    calls.task_log.get_task_ids_by_resume_ids.return_value = []
    service = CandidateErasureService(
        candidate_repo=calls.candidate, resume_repo=calls.resume, campaign_candidate_repo=calls.cc,
        consent_repo=MagicMock(), email_notification_repo=MagicMock(), celery_task_log_repo=calls.task_log,
        dead_letter_queue_repo=MagicMock(), storage_service=storage or calls.storage, audit_service=calls.audit,
        composite_score_history_repo=MagicMock(), notifier=MagicMock(),
        upload_decision_service=MagicMock(discard_for_erased_candidate=MagicMock(return_value=["held/x.pdf"])),
        bulk_upload_job_repo=calls.jobs, bulk_upload_job_file_repo=calls.files,
    )
    return service, candidate, calls


def test_files_are_deleted_only_after_the_commit():
    service, candidate, calls = _service()
    result = service.erase_candidate(candidate.id, actor_id="hr-1", actor_role="HR_ADMIN")
    order = [c[0] for c in calls.mock_calls if c[0] in ("candidate.commit", "storage.delete_file")]
    assert order[0] == "candidate.commit" and order.count("storage.delete_file") == 3
    assert result == {"files_deleted": 3, "files_pending_deletion": 0}
    assert calls.audit.log.call_count == 1
    assert calls.audit.log.call_args.kwargs["details"]["files_to_delete"] == 3


def test_a_failed_erasure_deletes_no_file():
    service, candidate, calls = _service()
    calls.consent_repo = None
    service.consent_repo = MagicMock(delete_by_candidate=MagicMock(side_effect=RuntimeError("db down")))
    with pytest.raises(RuntimeError):
        service.erase_candidate(candidate.id, actor_id="hr-1", actor_role="HR_ADMIN")
    calls.storage.delete_file.assert_not_called()
    calls.candidate.rollback.assert_called_once()


def test_a_failed_file_delete_is_retried_audited_and_reported():
    storage = MagicMock()
    storage.delete_file.side_effect = lambda bucket, path: (_ for _ in ()).throw(OSError("storage down")) \
        if path == "org/resume/b.pdf" else None
    service, candidate, calls = _service(storage=storage)
    with patch("app.tasks.compliance_tasks.queue_erased_file_deletes", return_value="t-retry") as queue:
        result = service.erase_candidate(candidate.id, actor_id="hr-1", actor_role="HR_ADMIN")
    assert result == {"files_deleted": 2, "files_pending_deletion": 1}
    assert queue.call_args.args[1:] == ("airs_resumes", ["org/resume/b.pdf"])
    follow_up = calls.audit.log.call_args_list[-1].kwargs["details"]
    assert follow_up == {"phase": "file_cleanup", "files_deleted": 2, "files_not_deleted": 1,
                         "retry_task_id": "t-retry"}
    assert calls.candidate.commit.call_count == 2           # the erasure, then the follow-up entry


def test_a_rebuilt_zip_is_removed_again_when_the_erasure_rolls_back():
    job = SimpleNamespace(id=uuid4(), zip_storage_path="campaign_x/bulk/job.zip")
    resume = SimpleNamespace(id=uuid4(), file_path="campaign_x/bulk-zip/j/0000_cv.pdf", original_filename="cv.pdf",
                             bulk_upload_job_id=job.id)
    service, candidate, calls = _service(resumes=[resume])
    calls.jobs.get_by_id.return_value = job
    calls.files.get_by_job_id.return_value = [
        SimpleNamespace(id=uuid4(), storage_path=resume.file_path, original_filename="cv.pdf", task_id="t1")]
    calls.storage.download_file.return_value = _zip("cv.pdf", "other.pdf")
    service.consent_repo = MagicMock(delete_by_candidate=MagicMock(side_effect=RuntimeError("db down")))
    with pytest.raises(RuntimeError):
        service.erase_candidate(candidate.id, actor_id="hr-1", actor_role="HR_ADMIN")
    new_zip = calls.storage.upload_file.call_args.kwargs["file_path"]
    deleted = [c.args[1] for c in calls.storage.delete_file.call_args_list]
    assert deleted == [new_zip]                    # only the orphan; the original ZIP is untouched


# --- the retry task ---------------------------------------------------------------------------

def _run_task(remaining_after, retries):
    from app.tasks import compliance_tasks as mod
    log = SimpleNamespace(task_id="t-1", queued_at=None)
    task_log_service = MagicMock()
    task_log_service.repository.get_by_task_id.return_value = log
    with patch.object(mod, "SessionLocal", return_value=MagicMock()), \
            patch.object(mod, "CeleryTaskLogService", return_value=task_log_service), \
            patch.object(mod, "CeleryTaskLogRepository"), \
            patch.object(mod, "StorageService"), \
            patch.object(mod, "_delete_all", return_value=remaining_after), \
            patch.object(mod, "dead_letter_paths") as dead_letter:
        mod.delete_erased_files.push_request(id="t-1", retries=retries)
        try:
            outcome = None
            try:
                mod.delete_erased_files(bucket="b", paths=["p1", "p2"])
            except Exception as exc:          # celery.exceptions.Retry
                outcome = exc
        finally:
            mod.delete_erased_files.pop_request()
    return outcome, task_log_service, dead_letter


def test_the_retry_task_succeeds_when_every_file_goes():
    outcome, log, dead_letter = _run_task([], retries=0)
    assert outcome is None
    log.mark_success.assert_called_once()
    dead_letter.assert_not_called()


def test_the_retry_task_retries_only_what_is_left():
    from celery.exceptions import Retry
    from app.tasks import compliance_tasks as mod
    with patch.object(mod.delete_erased_files, "retry", side_effect=Retry()) as retry:
        outcome, log, dead_letter = _run_task(["p2"], retries=1)
    assert isinstance(outcome, Retry)
    assert retry.call_args.kwargs["kwargs"] == {"bucket": "b", "paths": ["p2"]}
    assert retry.call_args.kwargs["countdown"] == 120
    dead_letter.assert_not_called()


def test_the_last_attempt_dead_letters_the_paths():
    from app.tasks.compliance_tasks import MAX_ATTEMPTS
    outcome, log, dead_letter = _run_task(["p2"], retries=MAX_ATTEMPTS - 1)
    assert outcome is None
    assert dead_letter.call_args.kwargs["paths"] == ["p2"]
    log.mark_failure.assert_called_once()


def test_a_failed_hand_off_dead_letters_the_paths_at_once():
    from app.tasks import compliance_tasks as mod
    task_log_service = MagicMock()
    task_log_service.create_log.return_value = SimpleNamespace(task_id="t-9")
    with patch.object(mod, "CeleryTaskLogService", return_value=task_log_service), \
            patch.object(mod, "CeleryTaskLogRepository"), \
            patch.object(mod, "dispatch_or_mark_lost", return_value=False), \
            patch.object(mod, "dead_letter_paths") as dead_letter:
        assert mod.queue_erased_file_deletes(MagicMock(), "b", ["p1"]) == "t-9"
    assert dead_letter.call_args.kwargs["paths"] == ["p1"]


def test_the_route_reports_pending_files():
    from app.api.routes import candidate_routes
    service = MagicMock()
    service.erase_candidate.return_value = {"files_deleted": 1, "files_pending_deletion": 2}
    with patch.object(candidate_routes, "authorising_role", return_value="HR_ADMIN"):
        response = candidate_routes.erase_candidate(uuid4(), reason=None, service=service,
                                                    user=SimpleNamespace(user_id="hr-1"))
    assert response.data == {"files_deleted": 1, "files_pending_deletion": 2}
    assert "2 stored file(s) could not be deleted yet" in response.message
