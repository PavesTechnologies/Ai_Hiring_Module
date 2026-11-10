"""
Review item 16: an embedding running during erasure can't write the resume's vector back.

Erasure row-locks the candidate's resumes before deleting anything; the embedding task re-checks its
resume under the same lock right before the insert and writes nothing when it is gone. The daily
reconciliation deletes embedding rows whose resume no longer exists. Mocked, plus compiled SQL; the
database checks are in tests/db/test_embedding_erasure_lock_db.py. No email.
"""
import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql

MODULE = "app.tasks.embedding_tasks"


def _sql(stmt) -> str:
    return str(stmt.compile(dialect=postgresql.dialect())).lower()


@pytest.mark.parametrize("reused", [True, False])
def test_the_task_writes_nothing_when_the_resume_was_erased_meanwhile(reused):
    from app.tasks import embedding_tasks as mod
    resume = SimpleNamespace(id=uuid4(), candidate_id=uuid4(),
                             parsed_json={"skills": [{"name": "Python"}], "work_experience": [], "education": []})
    repo = MagicMock()
    repo.get_by_id.return_value = resume
    repo.get_embedding_by_hash.return_value = SimpleNamespace(embedding=[0.0] * 384, is_anonymized=True) if reused else None
    repo.lock_if_exists.return_value = False
    task_log_service = MagicMock()
    task_log_service.repository = MagicMock()
    db = MagicMock()
    with patch.object(mod, "SessionLocal", return_value=db), \
            patch.object(mod, "ResumeRepository", return_value=repo), \
            patch.object(mod, "ConfigRepository"), \
            patch.object(mod, "CeleryTaskLogRepository") as logs, \
            patch.object(mod, "CeleryTaskLogService", return_value=task_log_service), \
            patch.object(mod, "CircuitBreakerRepository") as breakers, \
            patch.object(mod, "build_anonymized_embedding_text", return_value="python developer"), \
            patch.object(mod, "verify_anonymized_text", return_value=(True, None)), \
            patch.object(mod, "EmbeddingService") as embeddings:
        logs.return_value.get_by_task_id.return_value = None
        breakers.return_value.transition_to_half_open_if_due.return_value = SimpleNamespace(state=None)
        embeddings.return_value.generate_embeddings.return_value = [[0.1] * 384]
        mod.generate_resume_embedding_task.push_request(id="t-1", retries=0)
        try:
            mod.generate_resume_embedding_task(resume_id=str(resume.id))
        finally:
            mod.generate_resume_embedding_task.pop_request()
    repo.lock_if_exists.assert_called_once_with(resume.id)
    repo.create_resume_embedding.assert_not_called()
    repo.commit.assert_not_called()
    db.rollback.assert_called()
    summary = json.loads(task_log_service.mark_success.call_args.kwargs["summary"])
    assert summary["skipped"] is True and "erased" in summary["reason"]


def test_erasure_locks_the_resumes_before_deleting_their_embeddings():
    from app.services.compliance.candidate_erasure_service import CandidateErasureService
    candidate = SimpleNamespace(id=uuid4(), encryption_key_id=None, full_name_encrypted=None, email_encrypted=None,
                                phone_encrypted=None)
    resumes = [SimpleNamespace(id=uuid4(), file_path="r.pdf", original_filename="r.pdf", bulk_upload_job_id=None)]
    calls = MagicMock()
    calls.candidate.get_by_id.return_value = candidate
    calls.resume.get_all_versions_by_candidate.return_value = resumes
    calls.cc.get_by_candidate_id.return_value = []
    calls.task_log.find_task_ids_mentioning.return_value = []
    calls.task_log.get_task_ids_by_resume_ids.return_value = []
    service = CandidateErasureService(
        candidate_repo=calls.candidate, resume_repo=calls.resume, campaign_candidate_repo=calls.cc,
        consent_repo=MagicMock(), email_notification_repo=MagicMock(), celery_task_log_repo=calls.task_log,
        dead_letter_queue_repo=MagicMock(), storage_service=MagicMock(), audit_service=MagicMock(),
        composite_score_history_repo=MagicMock(), notifier=MagicMock(),
        upload_decision_service=MagicMock(discard_for_erased_candidate=MagicMock(return_value=[])))
    service.erase_candidate(candidate.id, actor_id="hr-1", actor_role="HR_ADMIN")
    order = [c[0] for c in calls.mock_calls
             if c[0] in ("resume.lock_for_erasure", "resume.delete_embeddings_by_candidate")]
    assert order == ["resume.lock_for_erasure", "resume.delete_embeddings_by_candidate"]
    calls.resume.lock_for_erasure.assert_called_once_with([resumes[0].id])


def test_the_lock_queries():
    from app.repositories.resume_repository import ResumeRepository
    db = MagicMock()
    ResumeRepository(db).lock_for_erasure([uuid4(), uuid4()])
    sql = _sql(db.execute.call_args.args[0])
    assert sql.startswith("select resumes.id") and "order by resumes.id" in sql and sql.endswith("for update")
    db2 = MagicMock()
    db2.execute.return_value.first.return_value = None
    assert ResumeRepository(db2).lock_if_exists(uuid4()) is False
    assert _sql(db2.execute.call_args.args[0]).endswith("for update")
    db3 = MagicMock()
    ResumeRepository(db3).lock_for_erasure([])
    db3.execute.assert_not_called()


def test_the_orphan_delete_targets_rows_without_a_resume():
    from app.repositories.resume_repository import ResumeRepository
    db = MagicMock()
    db.execute.return_value.rowcount = 2
    assert ResumeRepository(db).delete_orphaned_embeddings() == 2
    sql = _sql(db.execute.call_args.args[0])
    assert sql.startswith("delete from resume_embeddings") and "not (exists" in sql


def test_the_daily_reconciliation_deletes_orphans_and_reports_them():
    from app.tasks import talent_pool_tasks as mod
    repo = MagicMock()
    repo.delete_orphaned_embeddings.return_value = 3
    repo.get_distinct_candidate_ids_with_embeddings.return_value = []
    task_log_service = MagicMock()
    with patch.object(mod, "SessionLocal", return_value=MagicMock()), \
            patch.object(mod, "ResumeRepository", return_value=repo), \
            patch.object(mod, "CandidateRepository"), patch.object(mod, "CampaignCandidateRepository"), \
            patch.object(mod, "CeleryTaskLogRepository"), \
            patch.object(mod, "CeleryTaskLogService", return_value=task_log_service):
        mod.validate_talent_pool_eligibility()
    summary = json.loads(task_log_service.mark_success.call_args.kwargs["summary"])
    assert summary["orphaned_embeddings_deleted"] == 3
