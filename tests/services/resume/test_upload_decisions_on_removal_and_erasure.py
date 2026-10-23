"""
Pending upload decisions when the candidate leaves:

  removed from the campaign (delete_campaign_candidate)   pending decisions there
                                                          -> KEPT_EXISTING (rows kept
                                                          as history, audited "candidate
                                                          removed from campaign"), held
                                                          files deleted after the commit
  erased (CandidateErasureService.erase_candidate)        every decision row deleted
                                                          (any status); PENDING held
                                                          files deleted from storage

Repositories and storage mocked - nothing real is touched, no email.
"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

from app.enums.constants import ActionType, EntityType
from app.models.async_tasks import BulkUploadFileStatus
from app.models.upload_decisions import UploadDecisionStatus
from app.services.resume.upload_decision_service import UploadDecisionService


def _service():
    service = UploadDecisionService.__new__(UploadDecisionService)
    service.db = MagicMock()
    for name in ("resume_repo", "campaign_candidate_repo", "campaign_repo", "candidate_repo", "decision_repo",
                 "job_repo", "job_file_repo", "task_log_repo", "audit_service", "encryption_service"):
        setattr(service, name, MagicMock())
    service._storage_service = MagicMock()
    service.task_log_repo.get_by_task_id.return_value = None
    return service


def _decision(status=UploadDecisionStatus.PENDING, cc_id=None):
    now = datetime.now(timezone.utc)
    return SimpleNamespace(
        id=uuid4(), campaign_id=uuid4(), campaign_candidate_id=cc_id or uuid4(), candidate_id=uuid4(),
        bulk_upload_job_id=uuid4(), bulk_upload_job_file_id=uuid4(), storage_path=f"bulk/{uuid4()}.pdf",
        original_filename="new.pdf", status=status.value, decided_by=None, decided_at=None, new_resume_id=None,
        created_at=now, expires_at=now + timedelta(days=30),
    )


def _held_file(decision):
    return SimpleNamespace(id=decision.bulk_upload_job_file_id, bulk_upload_job_id=decision.bulk_upload_job_id,
                           status=BulkUploadFileStatus.NEEDS_DECISION, task_id=None)


# --- service --------------------------------------------------------------------

def test_removed_candidate_pending_decisions_become_kept_existing_and_are_audited():
    service = _service()
    cc_id = uuid4()
    decision = _decision(cc_id=cc_id)
    service.decision_repo.list_pending_by_campaign_candidate.return_value = [decision]
    service.job_file_repo.get_by_id.return_value = _held_file(decision)

    paths = service.close_pending_for_removed_campaign_candidate(cc_id, actor_id="hr-1", actor_role="HR_ADMIN")

    assert paths == [decision.storage_path]
    assert decision.status == UploadDecisionStatus.KEPT_EXISTING.value and decision.decided_by == "hr-1"
    service.decision_repo.delete.assert_not_called()              # kept as history
    service.job_file_repo.update_status.assert_called_once_with(decision.bulk_upload_job_file_id,
                                                                BulkUploadFileStatus.SKIPPED_DUPLICATE)
    audit = service.audit_service.log.call_args.kwargs
    assert audit["action_type"] == ActionType.RESUME_KEPT_EXISTING
    assert audit["entity_type"] == EntityType.UPLOAD_DECISION
    assert audit["details"]["reason"] == "candidate removed from campaign"
    service._storage_service.delete_file.assert_not_called()      # only after the caller commits
    service.db.commit.assert_not_called()


def test_erased_candidate_decisions_are_deleted_and_pending_files_returned():
    service = _service()
    pending = _decision()
    decided = _decision(status=UploadDecisionStatus.NEW_VERSION_USED)
    service.decision_repo.list_by_candidate.return_value = [pending, decided]
    service.job_file_repo.get_by_id.return_value = _held_file(pending)

    paths = service.discard_for_erased_candidate(pending.candidate_id)

    assert paths == [pending.storage_path]
    assert [c.args[0] for c in service.decision_repo.delete.call_args_list] == [pending, decided]
    service.job_file_repo.update_status.assert_called_once()
    service.db.commit.assert_not_called()


def test_held_file_deletion_is_best_effort():
    service = _service()
    service._storage_service.delete_file.side_effect = [RuntimeError("storage down"), None]
    service.delete_held_files(["a.pdf", "b.pdf"])
    assert service._storage_service.delete_file.call_count == 2


# --- delete_campaign_candidate ------------------------------------------------------

def _cc_service(decisions):
    from app.services.campaign.campaign_candidate_service import CampaignCandidateService
    cc = SimpleNamespace(id=uuid4(), campaign_id=uuid4(), candidate_id=uuid4(), resume_id=uuid4())
    repo = MagicMock()
    repo.get_by_id.return_value = cc
    order = []
    repo.commit.side_effect = lambda: order.append("commit")
    decisions.delete_held_files.side_effect = lambda paths: order.append(("delete_files", paths))
    service = CampaignCandidateService(
        campaign_repo=MagicMock(), campaign_candidate_repo=repo, audit_service=MagicMock(),
        email_notification_repo=MagicMock(), candidate_note_repo=MagicMock(), interview_schedule_repo=MagicMock(),
        composite_score_history_repo=MagicMock(), dead_letter_queue_repo=MagicMock(),
        celery_task_log_repo=MagicMock(), upload_decision_service=decisions,
    )
    return service, cc, order


def test_removing_a_candidate_closes_their_pending_decisions_then_deletes_the_files():
    decisions = MagicMock()
    decisions.close_pending_for_removed_campaign_candidate.return_value = ["bulk/held.pdf"]
    service, cc, order = _cc_service(decisions)
    with patch("app.services.campaign.campaign_candidate_service.CandidateChangeNotifier"):
        service.delete_campaign_candidate(cc.id, actor_id="hr-1", actor_role="HR_ADMIN")
    decisions.close_pending_for_removed_campaign_candidate.assert_called_once_with(
        cc.id, actor_id="hr-1", actor_role="HR_ADMIN")
    assert order == ["commit", ("delete_files", ["bulk/held.pdf"])]


def test_removing_a_candidate_without_pending_decisions_deletes_nothing():
    decisions = MagicMock()
    decisions.close_pending_for_removed_campaign_candidate.return_value = []
    service, cc, order = _cc_service(decisions)
    with patch("app.services.campaign.campaign_candidate_service.CandidateChangeNotifier"):
        service.delete_campaign_candidate(cc.id, actor_id="hr-1", actor_role="HR_ADMIN")
    decisions.delete_held_files.assert_not_called()


# --- erase_candidate ------------------------------------------------------------------

def test_erasure_discards_upload_decisions_and_deletes_held_files():
    from app.services.compliance.candidate_erasure_service import CandidateErasureService
    decisions = MagicMock()
    decisions.discard_for_erased_candidate.return_value = ["bulk/held.pdf"]
    storage = MagicMock()
    candidate_repo = MagicMock()
    candidate = SimpleNamespace(id=uuid4())
    candidate_repo.get_by_id.return_value = candidate
    resume_repo = MagicMock()
    resume_repo.get_all_versions_by_candidate.return_value = []
    campaign_candidate_repo = MagicMock()
    campaign_candidate_repo.get_by_candidate_id.return_value = []
    audit = MagicMock()
    service = CandidateErasureService(
        candidate_repo=candidate_repo, resume_repo=resume_repo, campaign_candidate_repo=campaign_candidate_repo,
        consent_repo=MagicMock(), email_notification_repo=MagicMock(), celery_task_log_repo=MagicMock(),
        dead_letter_queue_repo=MagicMock(), storage_service=storage, audit_service=audit,
        composite_score_history_repo=MagicMock(), notifier=MagicMock(), upload_decision_service=decisions,
    )

    service.erase_candidate(candidate.id, actor_id="hr-1", actor_role="HR_ADMIN")

    decisions.discard_for_erased_candidate.assert_called_once_with(candidate.id)
    storage.delete_file.assert_called_once_with("airs_resumes", "bulk/held.pdf")
    assert audit.log.call_args.kwargs["details"]["upload_decision_files_deleted"] == 1
    candidate_repo.commit.assert_called_once()
