"""
Candidate erasure also removes rows keyed to the resumes without a foreign key:

  - their processing-stage rows, and the stage-failure rows of those tasks;
  - the parse cache's LLM extraction of their text, found by the text hashes
    their own derived rows recorded - collected before those rows are deleted.

Repositories are mocked; the queries are compiled, never run. No email.
"""
import re
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql

from app.models.async_tasks import DocumentType
from app.services.compliance.candidate_erasure_service import CandidateErasureService


@pytest.fixture(autouse=True)
def no_email():
    with patch("app.services.notifications.ses_email_client.SESEmailClient.send_email",
               side_effect=AssertionError("email sent")):
        yield


def _service(resume_ids, **overrides):
    candidate = SimpleNamespace(id=uuid4(), encryption_key_id=uuid4(), full_name_encrypted=None,
                                email_encrypted=None, phone_encrypted=None)
    calls = MagicMock()                          # one manager: the order of calls across repositories
    calls.candidate.get_by_id.return_value = candidate
    calls.resume.get_all_versions_by_candidate.return_value = [
        SimpleNamespace(id=rid, file_path=f"r/{rid}.pdf", original_filename=None, bulk_upload_job_id=None)
        for rid in resume_ids]
    calls.cc.get_by_candidate_id.return_value = []
    calls.task_log.find_task_ids_mentioning.return_value = []
    calls.detection.resume_text_hashes.return_value = {"h1", "h2"}
    calls.detection.delete_parse_cache_by_text_hashes.return_value = 3
    calls.processing.delete_for_documents.return_value = 9
    calls.processing.delete_for_tasks.return_value = 0
    calls.task_log.get_task_ids_by_resume_ids.return_value = []
    kwargs = dict(
        candidate_repo=calls.candidate, resume_repo=calls.resume, campaign_candidate_repo=calls.cc,
        consent_repo=MagicMock(), email_notification_repo=MagicMock(), celery_task_log_repo=calls.task_log,
        dead_letter_queue_repo=MagicMock(), storage_service=MagicMock(), audit_service=calls.audit,
        composite_score_history_repo=MagicMock(), notifier=MagicMock(),
        upload_decision_service=MagicMock(discard_for_erased_candidate=MagicMock(return_value=[])),
        document_processing_repo=calls.processing, skill_detection_repo=calls.detection,
    )
    kwargs.update(overrides)
    return CandidateErasureService(**kwargs), candidate, calls


def test_stage_rows_and_cache_entries_go_and_are_counted():
    ids = [uuid4(), uuid4()]
    service, candidate, calls = _service(ids)

    service.erase_candidate(candidate.id, actor_id="hr-1", actor_role="HR_ADMIN")

    calls.detection.resume_text_hashes.assert_called_once_with(ids)
    calls.detection.delete_parse_cache_by_text_hashes.assert_called_once_with({"h1", "h2"})
    calls.processing.delete_for_documents.assert_called_once_with(DocumentType.RESUME, ids)
    details = calls.audit.log.call_args.kwargs["details"]
    assert details["parse_cache_entries_deleted"] == 3 and details["processing_stage_rows_deleted"] == 9
    calls.candidate.commit.assert_called_once()


def test_the_text_hashes_are_read_before_the_rows_that_record_them_are_deleted():
    ids = [uuid4()]
    service, candidate, calls = _service(ids)
    service.erase_candidate(candidate.id, actor_id="hr-1", actor_role="HR_ADMIN")
    order = [c[0] for c in calls.mock_calls
             if c[0] in ("detection.resume_text_hashes", "resume.delete_skill_detection_data")]
    assert order == ["detection.resume_text_hashes", "resume.delete_skill_detection_data"]


def test_without_the_repositories_erasure_still_works():
    service, candidate, calls = _service([uuid4()], document_processing_repo=None, skill_detection_repo=None)
    service.erase_candidate(candidate.id, actor_id="hr-1", actor_role="HR_ADMIN")
    details = calls.audit.log.call_args.kwargs["details"]
    assert details["parse_cache_entries_deleted"] == 0 and details["processing_stage_rows_deleted"] == 0
    calls.candidate.commit.assert_called_once()


# --- the queries ----------------------------------------------------------------------------

def _sql(stmt) -> str:
    return str(stmt.compile(dialect=postgresql.dialect())).lower()


def test_text_hashes_come_from_the_resume_and_its_derived_rows():
    from app.repositories.skill_detection_repository import SkillDetectionRepository
    db = MagicMock()
    stored, derived = MagicMock(), MagicMock()
    stored.scalars.return_value = [["h-parse"], None]                       # resumes.parse_text_hashes
    derived.scalars.return_value = ["h1", ""]
    db.execute.side_effect = [stored, derived, derived, derived]
    hashes = SkillDetectionRepository(db).resume_text_hashes([uuid4()])
    tables = [re.split(r"\sfrom\s", _sql(c.args[0]), maxsplit=1)[1].split()[0] for c in db.execute.call_args_list]
    assert tables == ["resumes", "resume_skill_evidence", "resume_provisional_skill_scans", "core_skill_confirmations"]
    assert hashes == {"h-parse", "h1"}                                        # empty hash dropped


def test_parse_cache_delete_is_by_text_hash_and_skips_nothing_to_do():
    from app.repositories.skill_detection_repository import SkillDetectionRepository
    db = MagicMock()
    db.execute.return_value.rowcount = 2
    assert SkillDetectionRepository(db).delete_parse_cache_by_text_hashes({"h1"}) == 2
    sql = _sql(db.execute.call_args.args[0])
    assert sql.startswith("delete from resume_parse_cache") and "resume_parse_cache.text_hash in" in sql
    db2 = MagicMock()
    assert SkillDetectionRepository(db2).delete_parse_cache_by_text_hashes(set()) == 0
    db2.execute.assert_not_called()


def test_stage_rows_delete_scopes_to_the_document_type_and_cleans_orphaned_failure_rows():
    from app.repositories.document_processing_repository import DocumentProcessingRepository
    db = MagicMock()
    task_lookup = MagicMock()
    task_lookup.scalars.return_value = ["t-1", "t-2"]
    deleted = MagicMock(rowcount=5)
    still_used = MagicMock()
    still_used.scalars.return_value = ["t-2"]                 # t-2 also processed another document
    db.execute.side_effect = [task_lookup, deleted, still_used, MagicMock()]

    assert DocumentProcessingRepository(db).delete_for_documents(DocumentType.RESUME, [uuid4()]) == 5

    statements = [c.args[0] for c in db.execute.call_args_list]
    stage_delete = _sql(statements[1])
    assert stage_delete.startswith("delete from document_processing_stage_executions")
    assert "document_type" in stage_delete and "document_id in" in stage_delete
    failure_delete = statements[3]
    assert _sql(failure_delete).startswith("delete from stage_failure_logs")
    assert failure_delete.compile(dialect=postgresql.dialect()).params["task_id_1"] == ["t-1"]


def test_the_di_provider_supplies_both_repositories():
    from app.dependencies.resume import get_candidate_erasure_service
    service = get_candidate_erasure_service(
        candidate_repo=MagicMock(), resume_repo=MagicMock(), campaign_candidate_repo=MagicMock(),
        consent_repo=MagicMock(), email_notification_repo=MagicMock(), celery_task_log_repo=MagicMock(),
        dead_letter_queue_repo=MagicMock(), storage_service=MagicMock(), audit_service=MagicMock(),
        composite_score_history_repo=MagicMock(), db=MagicMock(), upload_decision_service=MagicMock(),
    )
    assert service.document_processing_repo is not None and service.skill_detection_repo is not None
