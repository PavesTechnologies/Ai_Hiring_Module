"""
Review item 13: erasure removes a failed parse's stage rows and error text.

A parse links its stage rows to the resume only when it succeeds, and a failed stage stores str(exc),
which can quote the resume. Erasure now also deletes stage and stage-failure rows by the task ids of the
candidate's own parses: their resumes' task logs and their bulk job files. Repositories are mocked;
the queries are compiled, never run. No email.
"""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql

from app.services.compliance.candidate_erasure_service import CandidateErasureService


@pytest.fixture(autouse=True)
def no_email():
    with patch("app.services.notifications.ses_email_client.SESEmailClient.send_email",
               side_effect=AssertionError("email sent")):
        yield


def _sql(stmt) -> str:
    return str(stmt.compile(dialect=postgresql.dialect())).lower()


def _service(resumes, job_files=()):
    candidate = SimpleNamespace(id=uuid4(), encryption_key_id=uuid4(), full_name_encrypted=None,
                                email_encrypted=None, phone_encrypted=None)
    calls = MagicMock()
    calls.candidate.get_by_id.return_value = candidate
    calls.resume.get_all_versions_by_candidate.return_value = resumes
    calls.cc.get_by_candidate_id.return_value = []
    calls.task_log.find_task_ids_mentioning.return_value = []
    calls.task_log.get_task_ids_by_resume_ids.return_value = ["t-single"]
    calls.detection.resume_text_hashes.return_value = set()
    calls.detection.delete_parse_cache_by_text_hashes.return_value = 0
    calls.processing.delete_for_documents.return_value = 1
    calls.processing.delete_for_tasks.return_value = 4
    calls.files.get_by_job_id.return_value = list(job_files)
    calls.jobs.get_by_id.return_value = None
    service = CandidateErasureService(
        candidate_repo=calls.candidate, resume_repo=calls.resume, campaign_candidate_repo=calls.cc,
        consent_repo=MagicMock(), email_notification_repo=MagicMock(), celery_task_log_repo=calls.task_log,
        dead_letter_queue_repo=MagicMock(), storage_service=MagicMock(), audit_service=calls.audit,
        composite_score_history_repo=MagicMock(), notifier=MagicMock(),
        upload_decision_service=MagicMock(discard_for_erased_candidate=MagicMock(return_value=[])),
        bulk_upload_job_repo=calls.jobs, bulk_upload_job_file_repo=calls.files,
        document_processing_repo=calls.processing, skill_detection_repo=calls.detection,
    )
    return service, candidate, calls


def test_the_stage_rows_of_their_own_parse_tasks_go_linked_or_not():
    job_id = uuid4()
    single = SimpleNamespace(id=uuid4(), file_path="r/single.pdf", original_filename="a.pdf", bulk_upload_job_id=None)
    bulk = SimpleNamespace(id=uuid4(), file_path="campaign_x/bulk-zip/j/0001_b.pdf", original_filename="b.pdf",
                           bulk_upload_job_id=job_id)
    job_file = SimpleNamespace(id=uuid4(), storage_path=bulk.file_path, original_filename="b.pdf", task_id="t-bulk")
    service, candidate, calls = _service([single, bulk], [job_file])

    service.erase_candidate(candidate.id, actor_id="hr-1", actor_role="HR_ADMIN")

    calls.task_log.get_task_ids_by_resume_ids.assert_called_once_with([single.id, bulk.id])
    calls.processing.delete_for_tasks.assert_called_once_with(["t-bulk", "t-single"])
    details = calls.audit.log.call_args.kwargs["details"]
    assert details["processing_stage_rows_deleted"] == 5           # 1 linked + 4 by task
    # the task ids are read before the task-log rows go
    order = [c[0] for c in calls.mock_calls
             if c[0] in ("task_log.get_task_ids_by_resume_ids", "task_log.delete_by_resume_id")]
    assert order[0] == "task_log.get_task_ids_by_resume_ids"


def test_the_delete_by_task_removes_stage_and_failure_rows_of_exactly_those_tasks():
    from app.repositories.document_processing_repository import DocumentProcessingRepository
    db = MagicMock()
    db.execute.side_effect = [MagicMock(rowcount=3), MagicMock()]
    assert DocumentProcessingRepository(db).delete_for_tasks(["t-1", "", None, "t-2"]) == 3
    stage, failure = (c.args[0] for c in db.execute.call_args_list)
    assert _sql(stage).startswith("delete from document_processing_stage_executions")
    assert _sql(failure).startswith("delete from stage_failure_logs")
    for stmt in (stage, failure):
        assert stmt.compile(dialect=postgresql.dialect()).params["task_id_1"] == ["t-1", "t-2"]


def test_no_tasks_means_no_delete():
    from app.repositories.document_processing_repository import DocumentProcessingRepository
    db = MagicMock()
    assert DocumentProcessingRepository(db).delete_for_tasks([]) == 0
    db.execute.assert_not_called()


def test_the_task_ids_come_from_the_resumes_task_logs():
    from app.repositories.celery_task_log_repository import CeleryTaskLogRepository
    db = MagicMock()
    db.execute.return_value.scalars.return_value = ["t-1"]
    rid = uuid4()
    assert CeleryTaskLogRepository(db).get_task_ids_by_resume_ids([rid]) == ["t-1"]
    sql = _sql(db.execute.call_args.args[0])
    assert sql.startswith("select celery_task_log.task_id") and "celery_task_log.resume_id in" in sql
    assert CeleryTaskLogRepository(MagicMock()).get_task_ids_by_resume_ids([]) == []
