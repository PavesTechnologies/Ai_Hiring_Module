"""
Duplicate resume decisions - UploadDecisionService with every repository,
storage and Celery mocked: "use new version" (shared by single upload and a
bulk decision), who may decide, "keep existing" discarding the held file, and
the 30-day expiry. Never real storage, LLM, Celery or email.
"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from app.enums.constants import ActionType, EntityType
from app.exceptions.resume_exceptions import UploadDecisionForbiddenException, UploadDecisionNotPendingException
from app.models.async_tasks import BulkUploadFileStatus
from app.models.candidates import FileFormat, ParseStatus
from app.models.pipeline import PipelineStage
from app.models.upload_decisions import DECISION_TTL_DAYS, UploadDecisionStatus
from app.services.resume.upload_decision_service import UploadDecisionService, mask_email

TASKS = "app.tasks.resume_processing_tasks"


def _service():
    service = UploadDecisionService.__new__(UploadDecisionService)
    service.db = MagicMock()
    for name in ("resume_repo", "campaign_candidate_repo", "campaign_repo", "candidate_repo", "decision_repo",
                 "job_repo", "job_file_repo", "task_log_repo", "audit_service", "encryption_service"):
        setattr(service, name, MagicMock())
    service._storage_service = MagicMock()
    service.resume_repo.get_by_id.return_value = None
    service.candidate_repo.get_by_id.return_value = None
    service.resume_repo.create.side_effect = lambda resume: setattr(resume, "id", uuid4()) or resume
    service.encryption_service.decrypt.side_effect = ["Jane Doe", "jane@example.com"] * 10
    return service


def _campaign(name="Backend Engineer"):
    return SimpleNamespace(id=uuid4(), name=name, prompt_template_id=uuid4())


def _cc(campaign, stage=PipelineStage.SHORTLISTED):
    return SimpleNamespace(id=uuid4(), campaign_id=campaign.id, candidate_id=uuid4(), resume_id=uuid4(),
                           pipeline_stage=stage)


def _use_new_version(service, cc, campaign, **extra):
    return service.use_new_version(
        cc, campaign, storage_path="org_None/resume/new.pdf", file_format=FileFormat.PDF, file_hash="h-new",
        original_filename="new.pdf", file_size_bytes=100, page_count=2, uploaded_by="rec-1", actor_id="rec-1",
        actor_role="RECRUITER", **extra,
    )


# --- use new version ----------------------------------------------------------

def test_use_new_version_creates_next_active_version_and_repoints_only_this_campaign():
    service, campaign = _service(), _campaign()
    cc = _cc(campaign)
    old_resume_id = cc.resume_id
    previous = SimpleNamespace(id=old_resume_id, version_number=2)
    service.resume_repo.get_by_id.return_value = previous
    service.resume_repo.get_active_by_candidate.return_value = previous
    service.resume_repo.get_max_version_number.return_value = 2
    other = SimpleNamespace(id=uuid4(), campaign_id=uuid4(), score_out_of_date_at=None, score_out_of_date_reason=None)
    same = SimpleNamespace(id=cc.id, campaign_id=campaign.id, score_out_of_date_at=None, score_out_of_date_reason=None)
    service.campaign_candidate_repo.get_by_candidate_id.return_value = [same, other]

    with patch(f"{TASKS}.process_resume_document") as task, \
            patch("app.services.candidate_change_notifier.CandidateChangeNotifier"):
        resume, prev, task_id = _use_new_version(service, cc, campaign)

    # N+1, active, pending; the old version is only deactivated (kept as history).
    assert resume.version_number == 3 and resume.is_active_version is True
    assert resume.parse_status == ParseStatus.PENDING and resume.file_hash == "h-new"
    service.resume_repo.deactivate_active_version.assert_called_once_with(cc.candidate_id)
    service.resume_repo.delete.assert_not_called()
    assert prev is previous
    # This campaign's candidate now uses it; its stage is untouched.
    assert cc.resume_id == resume.id and cc.pipeline_stage == PipelineStage.SHORTLISTED
    # Other campaigns stamped out of date, this one not.
    assert other.score_out_of_date_at is not None and "Backend Engineer" in other.score_out_of_date_reason
    assert same.score_out_of_date_at is None
    # Audited with old/new ids and versions.
    audit = service.audit_service.log.call_args.kwargs
    assert audit["action_type"] == ActionType.RESUME_NEW_VERSION_USED
    assert audit["details"]["old_resume_id"] == str(old_resume_id) and audit["details"]["old_version_number"] == 2
    assert audit["details"]["new_resume_id"] == str(resume.id) and audit["details"]["new_version_number"] == 3
    assert audit["details"]["other_campaigns_marked_out_of_date"] == 1
    service.db.commit.assert_called_once()
    # Parsed by the normal pipeline in new-version mode (only this candidate re-scored afterwards).
    kwargs = task.apply_async.call_args.kwargs
    assert kwargs["kwargs"]["new_version"] == {"campaign_candidate_id": str(cc.id)}
    assert kwargs["task_id"] == str(task_id)
    log = service.task_log_repo.create.call_args.args[0]
    assert log.idempotency_key.endswith(f":new_version:{cc.id}") and log.resume_id == resume.id


def test_use_new_version_dispatch_failure_is_flagged_for_recovery_not_raised():
    service, campaign = _service(), _campaign()
    cc = _cc(campaign)
    service.resume_repo.get_active_by_candidate.return_value = None
    with patch(f"{TASKS}.process_resume_document") as task, \
            patch("app.services.candidate_change_notifier.CandidateChangeNotifier"):
        task.apply_async.side_effect = RuntimeError("broker down")
        resume, _, _ = _use_new_version(service, cc, campaign)
    assert resume.version_number == 1
    log = service.task_log_repo.create.call_args.args[0]
    assert log.dispatch_failed is True


def test_recovery_redispatch_keeps_new_version_mode():
    from app.tasks.resume_processing_tasks import _recovery_kwargs, new_version_idempotency_key
    resume_id, cc_id = uuid4(), uuid4()
    campaign = _campaign()
    log = SimpleNamespace(resume_id=resume_id, idempotency_key=new_version_idempotency_key(resume_id, cc_id))
    assert _recovery_kwargs(log, campaign)["new_version"] == {"campaign_candidate_id": str(cc_id)}
    plain = SimpleNamespace(resume_id=resume_id, idempotency_key=f"RESUME_DOCUMENT_PROCESSING:{resume_id}")
    assert "new_version" not in _recovery_kwargs(plain, campaign)


# --- bulk decisions -----------------------------------------------------------

def _decision(uploaded_by="rec-1", status=UploadDecisionStatus.PENDING, campaign=None, cc=None):
    campaign = campaign or _campaign()
    return SimpleNamespace(
        id=uuid4(), campaign_id=campaign.id, campaign_candidate_id=cc.id if cc else uuid4(),
        candidate_id=cc.candidate_id if cc else uuid4(), existing_resume_id=cc.resume_id if cc else uuid4(),
        bulk_upload_job_id=uuid4(), bulk_upload_job_file_id=uuid4(), storage_path="campaign_x/bulk-zip/j/f.pdf",
        original_filename="new.pdf", file_hash="h", file_format="PDF", file_size_bytes=10, page_count=1,
        uploaded_by=uploaded_by, status=status.value, decided_by=None, decided_at=None, new_resume_id=None,
        created_at=datetime.now(timezone.utc), expires_at=datetime.now(timezone.utc) + timedelta(days=30),
    )


def _job_file(decision):
    return SimpleNamespace(id=decision.bulk_upload_job_file_id, bulk_upload_job_id=decision.bulk_upload_job_id,
                           status=BulkUploadFileStatus.NEEDS_DECISION, task_id="t-1")


def test_hold_for_decision_expires_after_30_days():
    service = _service()
    service.decision_repo.create.side_effect = lambda d: d
    campaign = _campaign()
    cc = _cc(campaign)
    job = SimpleNamespace(id=uuid4(), campaign_id=campaign.id, uploaded_by="rec-1")
    job_file = SimpleNamespace(id=uuid4(), storage_path="p", original_filename="f.pdf")
    decision = service.hold_for_decision(job=job, job_file=job_file, cc=cc, file_hash="h",
                                         file_format=FileFormat.PDF, file_size_bytes=1, page_count=None)
    assert DECISION_TTL_DAYS == 30
    assert decision.status == "PENDING" and decision.campaign_candidate_id == cc.id
    assert decision.existing_resume_id == cc.resume_id and decision.storage_path == "p"
    remaining = decision.expires_at - datetime.now(timezone.utc)
    assert timedelta(days=29, hours=23) < remaining <= timedelta(days=30)


def test_keep_existing_by_uploader_discards_the_held_file():
    service = _service()
    decision = _decision(uploaded_by="rec-1")
    service.decision_repo.get_by_id_for_update.return_value = decision
    job_file = _job_file(decision)
    service.job_file_repo.get_by_id.return_value = job_file
    task_log = SimpleNamespace(output_summary="waiting")
    service.task_log_repo.get_by_task_id.return_value = task_log

    result = service.decide(decision.id, "keep_existing", actor_id="rec-1", actor_roles=["RECRUITER"])

    assert result.decision.status == UploadDecisionStatus.KEPT_EXISTING.value
    assert decision.decided_by == "rec-1" and decision.decided_at is not None
    service._storage_service.delete_file.assert_called_once_with("airs_resumes", decision.storage_path)
    service.job_file_repo.update_status.assert_called_once_with(job_file.id, BulkUploadFileStatus.SKIPPED_DUPLICATE)
    service.job_repo.decrement_needs_decision_count.assert_called_once()
    service.job_repo.increment_duplicate_count.assert_called_once()
    assert "kept the existing resume" in task_log.output_summary
    audit = service.audit_service.log.call_args.kwargs
    assert audit["action_type"] == ActionType.RESUME_KEPT_EXISTING
    assert audit["entity_type"] == EntityType.UPLOAD_DECISION and audit["entity_id"] == decision.id
    service.resume_repo.create.assert_not_called()


def test_hr_admin_can_decide_someone_elses_upload():
    service, campaign = _service(), _campaign()
    cc = _cc(campaign)
    decision = _decision(uploaded_by="rec-1", campaign=campaign, cc=cc)
    service.decision_repo.get_by_id_for_update.return_value = decision
    service.campaign_candidate_repo.get_by_id.return_value = cc
    service.campaign_repo.get_by_id.return_value = campaign
    service.resume_repo.get_active_by_candidate.return_value = None
    service.job_file_repo.get_by_id.return_value = _job_file(decision)

    with patch(f"{TASKS}.process_resume_document") as task, \
            patch("app.services.candidate_change_notifier.CandidateChangeNotifier"):
        result = service.decide(decision.id, "use_new_version", actor_id="hr-9", actor_roles=["HR_ADMIN"])

    assert result.decision.status == UploadDecisionStatus.NEW_VERSION_USED.value
    assert result.new_resume_id == cc.resume_id == decision.new_resume_id
    assert task.apply_async.call_args.kwargs["kwargs"]["new_version"] == {"campaign_candidate_id": str(cc.id)}
    new_resume = service.resume_repo.create.call_args.args[0]
    assert new_resume.file_path == decision.storage_path and new_resume.uploaded_by == "rec-1"
    assert new_resume.bulk_upload_job_id == decision.bulk_upload_job_id
    service.job_file_repo.update_status.assert_called_once_with(decision.bulk_upload_job_file_id,
                                                                BulkUploadFileStatus.PROCESSED)
    service.job_repo.increment_processed_count.assert_called_once()
    audit = service.audit_service.log.call_args.kwargs
    assert audit["entity_type"] == EntityType.UPLOAD_DECISION and audit["actor_role"] == "HR_ADMIN"
    service._storage_service.delete_file.assert_not_called()


def test_another_recruiter_cannot_decide():
    service = _service()
    decision = _decision(uploaded_by="rec-1")
    service.decision_repo.get_by_id_for_update.return_value = decision
    with pytest.raises(UploadDecisionForbiddenException) as exc:
        service.decide(decision.id, "keep_existing", actor_id="rec-2", actor_roles=["RECRUITER"])
    assert exc.value.status_code == 403
    assert decision.status == "PENDING"
    service._storage_service.delete_file.assert_not_called()
    service.db.commit.assert_not_called()


def test_an_already_decided_decision_cannot_be_decided_again():
    service = _service()
    decision = _decision(status=UploadDecisionStatus.KEPT_EXISTING)
    service.decision_repo.get_by_id_for_update.return_value = decision
    with pytest.raises(UploadDecisionNotPendingException):
        service.decide(decision.id, "use_new_version", actor_id="rec-1", actor_roles=["RECRUITER"])


def test_use_new_version_needs_the_candidate_still_in_the_campaign():
    service = _service()
    decision = _decision()
    service.decision_repo.get_by_id_for_update.return_value = decision
    service.campaign_candidate_repo.get_by_id.return_value = None
    with pytest.raises(UploadDecisionNotPendingException):
        service.decide(decision.id, "use_new_version", actor_id="rec-1", actor_roles=["RECRUITER"])
    service.resume_repo.create.assert_not_called()


def test_expiry_keeps_existing_discards_file_and_audits_as_system():
    service = _service()
    decision = _decision()
    service.decision_repo.list_expired_pending.return_value = [decision]
    service.job_file_repo.get_by_id.return_value = _job_file(decision)
    now = datetime.now(timezone.utc)

    assert service.expire_pending(now) == 1

    service.decision_repo.list_expired_pending.assert_called_once_with(now)
    assert decision.status == UploadDecisionStatus.EXPIRED_KEPT_EXISTING.value and decision.decided_by is None
    service._storage_service.delete_file.assert_called_once_with("airs_resumes", decision.storage_path)
    audit = service.audit_service.log.call_args.kwargs
    assert audit["actor_id"] is None and audit["actor_role"] == "SYSTEM"
    assert audit["action_type"] == ActionType.UPLOAD_DECISION_EXPIRED
    assert audit["entity_type"] == EntityType.UPLOAD_DECISION


def test_storage_failure_on_discard_never_undoes_the_decision():
    service = _service()
    decision = _decision()
    service.decision_repo.get_by_id_for_update.return_value = decision
    service.job_file_repo.get_by_id.return_value = None
    service._storage_service.delete_file.side_effect = RuntimeError("storage down")
    result = service.decide(decision.id, "keep_existing", actor_id="rec-1", actor_roles=["RECRUITER"])
    assert result.decision.status == "KEPT_EXISTING"
    service.db.commit.assert_called_once()


def test_decision_items_show_name_and_masked_email():
    service = _service()
    decision = _decision()
    service.decision_repo.list_by_campaign.return_value = [decision]
    service.candidate_repo.get_by_id.return_value = SimpleNamespace(
        full_name_encrypted=b"n", email_encrypted=b"e", encryption_key_id=uuid4(), is_pii_deleted=False,
    )
    service.resume_repo.get_by_id.return_value = SimpleNamespace(original_filename="old.pdf",
                                                                 created_at=datetime.now(timezone.utc))
    [item] = service.list_for_campaign(decision.campaign_id, "PENDING")
    assert item.candidate_name == "Jane Doe" and item.candidate_email_masked == mask_email("jane@example.com")
    assert item.existing_original_filename == "old.pdf" and item.new_original_filename == "new.pdf"
    assert item.status == "PENDING" and item.expires_at == decision.expires_at


# --- the audit records the role that authorised the decision ---------------------------------

def test_uploader_with_general_listed_first_is_audited_as_recruiter():
    service = _service()
    decision = _decision(uploaded_by="rec-1")
    service.decision_repo.get_by_id_for_update.return_value = decision
    service.job_file_repo.get_by_id.return_value = _job_file(decision)
    service.decide(decision.id, "keep_existing", actor_id="rec-1", actor_roles=["General", "RECRUITER"])
    assert service.audit_service.log.call_args.kwargs["actor_role"] == "RECRUITER"


def test_hr_admin_uploader_is_audited_as_hr_admin():
    service = _service()
    decision = _decision(uploaded_by="rec-1")
    service.decision_repo.get_by_id_for_update.return_value = decision
    service.job_file_repo.get_by_id.return_value = _job_file(decision)
    service.decide(decision.id, "keep_existing", actor_id="rec-1", actor_roles=["General", "RECRUITER", "HR_ADMIN"])
    assert service.audit_service.log.call_args.kwargs["actor_role"] == "HR_ADMIN"


def test_decision_actor_role():
    from app.services.resume.upload_decision_service import decision_actor_role
    assert decision_actor_role(["General", "HR_ADMIN"], is_uploader=False) == "HR_ADMIN"
    assert decision_actor_role(["General", "RECRUITER"], is_uploader=True) == "RECRUITER"
    assert decision_actor_role(["General", "RECRUITER"], is_uploader=False) is None
    assert decision_actor_role(["General"], is_uploader=True) is None
    assert decision_actor_role(None, is_uploader=True) is None


def test_duplicate_skipped_records_its_source():
    service = _service()
    cc = _cc(_campaign())
    service.log_duplicate_skipped(cc, filename="f.pdf", file_hash="h", actor_id="rec-1", actor_role="RECRUITER")
    assert service.audit_service.log.call_args.kwargs["details"]["source"] == "single_upload"
    service.log_duplicate_skipped(cc, filename="f.pdf", file_hash="h", actor_id="rec-1", actor_role="RECRUITER",
                                  bulk_upload_job_id=uuid4(), source="bulk_upload_worker")
    audit = service.audit_service.log.call_args.kwargs
    assert audit["details"]["source"] == "bulk_upload_worker" and audit["actor_role"] == "RECRUITER"


def test_uploader_role_reads_the_local_user_record():
    from app.models.identity import UserRole
    service = _service()
    service.db.get.return_value = SimpleNamespace(role=UserRole.RECRUITER)
    assert service.uploader_role("rec-1") == "RECRUITER"
    service.db.get.return_value = None
    assert service.uploader_role("gone") is None
    assert service.uploader_role(None) is None
