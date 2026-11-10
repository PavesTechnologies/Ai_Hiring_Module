"""
Candidate erasure also removes what the row-by-row cascade missed:

  - the bulk-upload job-file rows of the candidate's files (uploaded file names),
    and a stored copy that isn't one of their resume files (a skipped duplicate);
  - their entries in the stored ZIP - rebuilt without them, or deleted when
    nothing else remains; a storage failure never blocks the DB erasure;
  - task-log rows that quote their name, email or phone (EMAIL_SEND included),
    or that belong to their own bulk files - and the dead-letter rows of those
    tasks first (FK), plus any dead-letter row quoting them.

Review item 15: their files are found by storage path (or, for a skipped identical duplicate, by
content hash) and their ZIP entries by staged position - never by file name, so another person's
"CV.pdf" in the same ZIP stays.

Repositories, storage and encryption are mocked; the two search queries are
compiled, never run. No database, no email.
"""
import hashlib
import io
import json
import zipfile
from types import SimpleNamespace
from unittest.mock import MagicMock, call
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql

from app.models.async_tasks import BulkUploadFileStatus
from app.services.compliance.candidate_erasure_service import CandidateErasureService

NAME, EMAIL, PHONE = "Jane Example", "jane.example@example.invalid", "+44 7700 900123"
CV = "CV.pdf"
CV_BYTES = b"jane's cv bytes"


def _zip(*entries) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("batch/", "")
        for name in entries:
            archive.writestr(f"batch/{name}", f"content of {name}")
    return buffer.getvalue()


def _names_in(zip_bytes: bytes) -> list[str]:
    return sorted(e.filename for e in zipfile.ZipFile(io.BytesIO(zip_bytes)).infolist() if not e.is_dir())


class _Harness:
    # The ZIP: their CV, another person's CV with the same name (other folder), and a third file.
    ZIP_ENTRIES = ("jane/CV.pdf", "raj/CV.pdf", "someone_else.pdf")

    def __init__(self, zip_entries=ZIP_ENTRIES, storage_error=None):
        self.job = SimpleNamespace(id=uuid4(), zip_storage_path="campaign_x/bulk/job.zip")
        self.candidate = SimpleNamespace(id=uuid4(), encryption_key_id=uuid4(), full_name_encrypted=b"n",
                                         email_encrypted=b"e", phone_encrypted=b"p")
        bulk_resume = SimpleNamespace(id=uuid4(), file_path="campaign_x/bulk-zip/j/0000_CV.pdf",
                                      original_filename=CV, bulk_upload_job_id=self.job.id,
                                      file_hash=hashlib.md5(CV_BYTES).hexdigest())
        single_resume = SimpleNamespace(id=uuid4(), file_path="org_None/resume/2.pdf",
                                        original_filename="jane_v2.docx", bulk_upload_job_id=None, file_hash="x")
        self.mine = SimpleNamespace(id=uuid4(), storage_path=bulk_resume.file_path, original_filename=CV,
                                    task_id="t-mine", status=BulkUploadFileStatus.PROCESSED)
        self.same_name = SimpleNamespace(id=uuid4(), storage_path="campaign_x/bulk-zip/j/0001_CV.pdf",
                                         original_filename=CV, task_id="t-raj", status=BulkUploadFileStatus.PROCESSED)
        self.other = SimpleNamespace(id=uuid4(), storage_path="campaign_x/bulk-zip/j/0002_someone_else.pdf",
                                     original_filename="someone_else.pdf", task_id="t-other",
                                     status=BulkUploadFileStatus.PROCESSED)

        self.candidate_repo = MagicMock()
        self.candidate_repo.get_by_id.return_value = self.candidate
        self.resume_repo = MagicMock()
        self.resume_repo.get_all_versions_by_candidate.return_value = [bulk_resume, single_resume]
        self.cc_repo = MagicMock()
        self.cc_repo.get_by_candidate_id.return_value = []
        self.job_repo = MagicMock()
        self.job_repo.get_by_id.return_value = self.job
        self.job_file_repo = MagicMock()
        self.job_file_repo.get_by_job_id.return_value = [self.mine, self.same_name, self.other]
        self.storage = MagicMock()
        self.files = {"campaign_x/bulk/job.zip": _zip(*zip_entries)}
        self.storage.download_file.side_effect = (
            (lambda bucket, path: (_ for _ in ()).throw(storage_error)) if storage_error
            else (lambda bucket, path: self.files[path]))
        self.encryption = MagicMock()
        self.encryption.decrypt.side_effect = lambda blob, key: {b"n": NAME, b"e": EMAIL, b"p": PHONE}[blob]
        # one manager so the order of the task-log / dead-letter deletes can be checked
        self.logs = MagicMock()
        self.logs.task_log.find_task_ids_mentioning.return_value = ["t-email", "t-mine"]
        self.logs.task_log.get_task_ids_by_resume_ids.return_value = []
        self.audit = MagicMock()
        self.service = CandidateErasureService(
            candidate_repo=self.candidate_repo, resume_repo=self.resume_repo, campaign_candidate_repo=self.cc_repo,
            consent_repo=MagicMock(), email_notification_repo=MagicMock(), celery_task_log_repo=self.logs.task_log,
            dead_letter_queue_repo=self.logs.dlq, storage_service=self.storage, audit_service=self.audit,
            composite_score_history_repo=MagicMock(), notifier=MagicMock(), upload_decision_service=MagicMock(
                discard_for_erased_candidate=MagicMock(return_value=[])),
            encryption_service=self.encryption, bulk_upload_job_repo=self.job_repo,
            bulk_upload_job_file_repo=self.job_file_repo,
        )

    def erase(self):
        self.service.erase_candidate(self.candidate.id, actor_id="hr-1", actor_role="HR_ADMIN")
        return self.audit.log.call_args_list[0].kwargs["details"]


def test_only_their_own_job_files_go_never_one_with_the_same_name():
    h = _Harness()
    h.erase()
    h.job_file_repo.delete_by_ids.assert_called_once_with([h.mine.id])
    deleted = [c.args[1] for c in h.storage.delete_file.call_args_list]
    assert h.same_name.storage_path not in deleted and h.other.storage_path not in deleted
    h.candidate_repo.commit.assert_called_once()


def test_a_skipped_identical_copy_of_their_resume_goes_too():
    h = _Harness()
    copy = SimpleNamespace(id=uuid4(), storage_path="campaign_x/bulk-zip/j/0003_copy.pdf", original_filename="copy.pdf",
                           task_id="t-copy", status=BulkUploadFileStatus.SKIPPED_DUPLICATE)
    not_theirs = SimpleNamespace(id=uuid4(), storage_path="campaign_x/bulk-zip/j/0004_x.pdf", original_filename="x.pdf",
                                 task_id="t-x", status=BulkUploadFileStatus.SKIPPED_DUPLICATE)
    h.files[copy.storage_path] = CV_BYTES
    h.files[not_theirs.storage_path] = b"someone else's bytes"
    h.job_file_repo.get_by_job_id.return_value = [h.mine, h.same_name, h.other, copy, not_theirs]
    h.files["campaign_x/bulk/job.zip"] = _zip(*_Harness.ZIP_ENTRIES, "copy.pdf", "x.pdf")
    h.erase()
    h.job_file_repo.delete_by_ids.assert_called_once_with([h.mine.id, copy.id])
    deleted = [c.args[1] for c in h.storage.delete_file.call_args_list]
    assert copy.storage_path in deleted and not_theirs.storage_path not in deleted


def test_the_zip_is_rebuilt_without_their_entry_only():
    h = _Harness()
    details = h.erase()

    upload = h.storage.upload_file.call_args.kwargs
    assert _names_in(upload["file_content"]) == ["batch/raj/CV.pdf", "batch/someone_else.pdf"]
    assert upload["bucket_name"] == "airs_resumes" and upload["file_path"].startswith("campaign_x/bulk/")
    assert h.job.zip_storage_path == upload["file_path"] != "campaign_x/bulk/job.zip"
    assert call("airs_resumes", "campaign_x/bulk/job.zip") in h.storage.delete_file.call_args_list
    assert details["bulk_zips_rebuilt"] == 1 and details["bulk_zips_deleted"] == 0


def test_a_zip_holding_only_their_files_is_deleted():
    h = _Harness(zip_entries=("jane/CV.pdf",))
    h.job_file_repo.get_by_job_id.return_value = [h.mine]
    details = h.erase()
    h.storage.upload_file.assert_not_called()
    assert call("airs_resumes", "campaign_x/bulk/job.zip") in h.storage.delete_file.call_args_list
    assert h.job.zip_storage_path is None
    assert details["bulk_zips_deleted"] == 1


def test_an_older_staged_file_is_matched_by_name_only_when_the_name_is_unique():
    h = _Harness(zip_entries=("jane/CV.pdf", "someone_else.pdf"))
    h.mine.storage_path = h.resume_repo.get_all_versions_by_candidate.return_value[0].file_path = \
        "campaign_x/bulk-zip/j/3f2a9c1e-0000-4000-8000-000000000000_CV.pdf"
    h.job_file_repo.get_by_job_id.return_value = [h.mine, h.other]
    h.erase()
    assert _names_in(h.storage.upload_file.call_args.kwargs["file_content"]) == ["batch/someone_else.pdf"]


def test_an_older_staged_file_with_a_shared_name_leaves_the_zip_alone():
    h = _Harness()
    h.mine.storage_path = h.resume_repo.get_all_versions_by_candidate.return_value[0].file_path = \
        "campaign_x/bulk-zip/j/3f2a9c1e-0000-4000-8000-000000000000_CV.pdf"
    details = h.erase()
    h.storage.upload_file.assert_not_called()
    assert h.job.zip_storage_path == "campaign_x/bulk/job.zip"
    assert details["bulk_zip_failures"] == 1
    h.candidate_repo.commit.assert_called_once()


def test_a_zip_without_their_files_is_left_alone():
    h = _Harness(zip_entries=("someone_else.pdf",))
    h.job_file_repo.get_by_job_id.return_value = [h.other]
    h.erase()
    h.storage.upload_file.assert_not_called()
    assert h.job.zip_storage_path == "campaign_x/bulk/job.zip"


def test_a_storage_failure_never_blocks_the_erasure():
    h = _Harness(storage_error=RuntimeError("storage down"))
    details = h.erase()
    assert details["bulk_zip_failures"] == 1
    h.candidate_repo.delete.assert_called_once_with(h.candidate)
    h.candidate_repo.commit.assert_called_once()


def test_task_logs_naming_them_or_of_their_files_go_with_their_dead_letters_first():
    h = _Harness()
    details = h.erase()

    find = h.logs.task_log.find_task_ids_mentioning.call_args
    assert find.args == ([NAME, EMAIL, PHONE],) and not find.kwargs      # no file-name search any more
    order = [c[0] for c in h.logs.mock_calls if c[0] in ("dlq.delete_for_erasure", "task_log.delete_by_task_ids")]
    assert order == ["dlq.delete_for_erasure", "task_log.delete_by_task_ids"]
    h.logs.dlq.delete_for_erasure.assert_called_once_with(["t-email", "t-mine"], [NAME, EMAIL, PHONE])
    h.logs.task_log.delete_by_task_ids.assert_called_once_with(["t-email", "t-mine"])
    assert details["task_logs_naming_candidate_deleted"] == 2             # t-raj, t-other stay


def test_a_held_files_job_is_searched_too():
    h = _Harness()
    held_job = uuid4()
    held = f"campaign_x/bulk-zip/{held_job}/0000_new_cv.pdf"
    h.service.upload_decision_service.discard_for_erased_candidate.return_value = [held]
    h.erase()
    searched = [c.args[0] for c in h.job_file_repo.get_by_job_id.call_args_list]
    assert held_job in searched and h.job.id in searched


def test_the_erasure_audit_entry_holds_counts_not_their_details():
    details = _Harness().erase()
    dumped = json.dumps(details, default=str)
    for value in (NAME, EMAIL, PHONE, CV, "jane_v2"):
        assert value not in dumped


def test_a_value_too_short_to_search_safely_is_not_searched():
    h = _Harness()
    h.encryption.decrypt.side_effect = lambda blob, key: {b"n": "Li", b"e": EMAIL, b"p": "123"}[blob]
    h.erase()
    assert h.logs.task_log.find_task_ids_mentioning.call_args.args[0] == [EMAIL]


def test_without_the_new_dependencies_erasure_still_works():
    h = _Harness()
    h.service.encryption_service = h.service.bulk_upload_job_repo = h.service.bulk_upload_job_file_repo = None
    h.erase()
    h.logs.task_log.find_task_ids_mentioning.assert_called_once()
    assert h.logs.task_log.find_task_ids_mentioning.call_args.args[0] == []
    h.candidate_repo.commit.assert_called_once()


def test_extraction_and_erasure_agree_on_entry_positions():
    from app.services.resume.bulk_zip_entries import real_entries, staged_entry_index
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name in ("a/", "__MACOSX/a/._x.pdf", "a/.hidden", "a/CV.pdf", "b/CV.pdf"):
            archive.writestr(name, "x")
    entries = real_entries(zipfile.ZipFile(io.BytesIO(buffer.getvalue())))
    assert [e.filename for e, _ in entries] == ["a/CV.pdf", "b/CV.pdf"]
    assert staged_entry_index("campaign_x/bulk-zip/j/0001_CV.pdf") == 1
    assert staged_entry_index("campaign_x/bulk-zip/j/3f2a9c1e-0000-4000-8000-000000000000_CV.pdf") is None


# --- the two search queries ------------------------------------------------------------------

def _sql(stmt) -> str:
    return str(stmt.compile(dialect=postgresql.dialect())).lower()


def test_task_log_search_matches_text_columns_and_scopes_file_names_to_their_jobs():
    from app.repositories.celery_task_log_repository import CeleryTaskLogRepository
    db = MagicMock()
    job_id = uuid4()
    CeleryTaskLogRepository(db).find_task_ids_mentioning([EMAIL], scoped_terms=["Jane_Example_CV.pdf"],
                                                         bulk_upload_job_ids=[job_id])
    stmt = db.execute.call_args.args[0]
    sql = _sql(stmt)
    for column in ("title", "output_summary", "error_message"):
        assert f"celery_task_log.{column} ilike" in sql
    assert "celery_task_log.bulk_upload_job_id in" in sql
    params = stmt.compile(dialect=postgresql.dialect()).params
    assert f"%{EMAIL}%" in params.values()
    assert "%Jane\\_Example\\_CV.pdf%" in params.values()          # LIKE wildcards in a file name are literal


def test_task_log_search_never_matches_file_names_outside_their_jobs():
    from app.repositories.celery_task_log_repository import CeleryTaskLogRepository
    db = MagicMock()
    assert CeleryTaskLogRepository(db).find_task_ids_mentioning([], scoped_terms=[CV], bulk_upload_job_ids=[]) == []
    db.execute.assert_not_called()


def test_dead_letter_cleanup_covers_their_tasks_and_rows_quoting_them():
    from app.repositories.dead_letter_queue_repository import DeadLetterQueueRepository
    db = MagicMock()
    DeadLetterQueueRepository(db).delete_for_erasure(["t-1"], [EMAIL])
    sql = _sql(db.execute.call_args.args[0])
    assert sql.startswith("delete from dead_letter_queue")
    assert "dead_letter_queue.original_task_id in" in sql
    for column in ("final_error_message", "full_error_trace", "input_payload"):
        assert column in sql


def test_the_di_provider_supplies_the_new_dependencies():
    from app.dependencies.resume import get_candidate_erasure_service
    service = get_candidate_erasure_service(
        candidate_repo=MagicMock(), resume_repo=MagicMock(), campaign_candidate_repo=MagicMock(),
        consent_repo=MagicMock(), email_notification_repo=MagicMock(), celery_task_log_repo=MagicMock(),
        dead_letter_queue_repo=MagicMock(), storage_service=MagicMock(), audit_service=MagicMock(),
        composite_score_history_repo=MagicMock(), db=MagicMock(), upload_decision_service=MagicMock(),
    )
    assert service.encryption_service is not None
    assert service.bulk_upload_job_repo is not None and service.bulk_upload_job_file_repo is not None


@pytest.fixture(autouse=True)
def no_email():
    from unittest.mock import patch
    with patch("app.services.notifications.ses_email_client.SESEmailClient.send_email",
               side_effect=AssertionError("email sent")):
        yield
