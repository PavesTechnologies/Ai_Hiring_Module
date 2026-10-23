"""
Duplicate resume decisions in the bulk upload (parse_bulk_upload_file), with
every repository, the parse stages and storage mocked - never a real LLM,
storage, Celery or email:

  identical file already in this campaign        SKIPPED_DUPLICATE, duplicate_count
  same email already in this campaign            NEEDS_DECISION after identity
                                                 extraction - no Resume created or
                                                 deactivated, no candidate change
  known candidate, campaign they are not in yet  new version + new campaign
                                                 candidate, other campaigns stamped
plus job finalisation counting NEEDS_DECISION, and the file log.
"""
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

from app.models.async_tasks import BulkUploadFileStatus, BulkUploadStatus
from app.models.candidates import FileFormat
from app.services.resume.file_validation_service import FileValidationResult

TASKS_MODULE = "app.tasks.bulk_upload_tasks"


class _Harness:
    def __init__(self):
        self.campaign_id = uuid4()
        self.job = SimpleNamespace(id=uuid4(), campaign_id=self.campaign_id, uploaded_by="uploader-1",
                                   jurisdiction="GLOBAL", ip_address=None, user_agent=None)
        self.campaign = SimpleNamespace(id=self.campaign_id, name="Backend Engineer", prompt_template_id=uuid4(),
                                        scoring_v2_enabled=False)
        self.job_file = SimpleNamespace(id=uuid4(), bulk_upload_job_id=self.job.id, storage_path="campaign/bulk/f.pdf",
                                        original_filename="f.pdf")

        self.file_repo = MagicMock()
        self.file_repo.get_by_id.return_value = self.job_file
        self.file_repo.try_start_processing.return_value = True
        self.job_repo = MagicMock()
        self.job_repo.get_by_id.side_effect = lambda _id: self.job if self._first_job_read() else None
        self._job_reads = 0
        self.candidate_repo = MagicMock()
        self.candidate_repo.get_by_email_hash.return_value = None
        self.resume_repo = MagicMock()
        self.resume_repo.get_by_file_hash_global.return_value = None
        self.resume_repo.get_active_by_candidate.return_value = None
        self.resume_repo.create.side_effect = lambda r: setattr(r, "id", uuid4()) or r
        self.campaign_repo = MagicMock()
        self.campaign_repo.get_by_id.return_value = self.campaign
        self.cc_repo = MagicMock()
        self.cc_repo.get_by_campaign_and_file_hash.return_value = None
        self.cc_repo.get_by_campaign_and_candidate.return_value = None
        self.cc_repo.get_by_candidate_id.return_value = []
        self.task_log_repo = MagicMock()
        self.task_log_repo.get_by_task_id.return_value = None
        self.checkpoint_repo = MagicMock()
        self.checkpoint_repo.get.return_value = None
        self.storage = MagicMock()
        self.storage.download_file.return_value = b"%PDF-1.4 fake"
        self.validation = MagicMock()
        self.validation.validate.return_value = FileValidationResult(file_format=FileFormat.PDF, size_bytes=13)
        self.cc_service = MagicMock()
        self.cc_service.create_campaign_candidate.return_value = SimpleNamespace(id=uuid4())
        self.candidate_service = MagicMock()
        self.decisions = MagicMock()
        self.pipeline = MagicMock()
        self.identity = ("Jane Doe", "jane@example.com", None)

    def _first_job_read(self):
        # The task body reads the job once; _maybe_finalize_job's read gets None.
        self._job_reads += 1
        return self._job_reads == 1

    def __enter__(self):
        m = TASKS_MODULE
        self._patches = [
            patch(f"{m}.SessionLocal", return_value=MagicMock()),
            patch(f"{m}.BulkUploadJobFileRepository", return_value=self.file_repo),
            patch(f"{m}.BulkUploadJobRepository", return_value=self.job_repo),
            patch(f"{m}.ConfigRepository", return_value=MagicMock()),
            patch(f"{m}.CandidateRepository", return_value=self.candidate_repo),
            patch(f"{m}.ResumeRepository", return_value=self.resume_repo),
            patch(f"{m}.SkillRepository", return_value=MagicMock()),
            patch(f"{m}.EncryptionKeyRepository", return_value=MagicMock()),
            patch(f"{m}.ConsentRepository", return_value=MagicMock()),
            patch(f"{m}.CampaignRepository", return_value=self.campaign_repo),
            patch(f"{m}.PromptTemplateRepository", return_value=MagicMock()),
            patch(f"{m}.CampaignCandidateRepository", return_value=self.cc_repo),
            patch(f"{m}.AuditRepository", return_value=MagicMock()),
            patch(f"{m}.CeleryTaskLogRepository", return_value=self.task_log_repo),
            patch(f"{m}.CheckpointRepository", return_value=self.checkpoint_repo),
            patch(f"{m}.StageFailureLogRepository", return_value=MagicMock()),
            patch(f"{m}.DeadLetterQueueRepository", return_value=MagicMock()),
            patch(f"{m}.DocumentProcessingRepository", return_value=MagicMock()),
            patch(f"{m}.CampaignCandidateService", return_value=self.cc_service),
            patch(f"{m}.CandidateService", return_value=self.candidate_service),
            patch(f"{m}.StorageService", return_value=self.storage),
            patch(f"{m}.FileValidationService", return_value=self.validation),
            patch(f"{m}.UploadDecisionService", return_value=self.decisions),
            patch(f"{m}.CandidateChangeNotifier", return_value=MagicMock()),
            patch(f"{m}.resolve_active_provider", return_value=MagicMock()),
            patch(f"{m}.LLMExtractionService", return_value=MagicMock()),
            patch(f"{m}.EmbeddingService", return_value=MagicMock()),
            patch(f"{m}.SkillNormalizationService", return_value=MagicMock()),
            patch(f"{m}.CacheService", return_value=MagicMock()),
            patch(f"{m}.get_redis_client", return_value=MagicMock()),
            patch(f"{m}.build_v2_detection", return_value=(MagicMock(), MagicMock())),
            patch(f"{m}.StageExecutionService", return_value=MagicMock()),
            patch(f"{m}.ResumeProcessingPipeline", return_value=self.pipeline),
            patch(f"{m}._resolve_bulk_candidate_identity", side_effect=lambda ctx: self.identity),
            patch(f"{m}.TextExtractionService.get_pdf_page_count", return_value=1),
            patch(f"{m}._enqueue_resume_embedding"),
            patch(f"{m}._enqueue_deterministic_scoring"),
        ]
        for p in self._patches:
            p.start()
        return self

    def __exit__(self, *exc_info):
        for p in reversed(self._patches):
            p.stop()

    def run(self):
        from app.tasks.bulk_upload_tasks import parse_bulk_upload_file
        parse_bulk_upload_file(task_id=str(uuid4()), bulk_upload_job_file_id=str(self.job_file.id))


def _status_updates(h):
    return [c.args[1] for c in h.file_repo.update_status.call_args_list]


def test_identical_file_in_campaign_is_skipped_duplicate():
    with _Harness() as h:
        existing = SimpleNamespace(id=uuid4(), campaign_id=h.campaign_id, resume_id=uuid4())
        h.cc_repo.get_by_campaign_and_file_hash.return_value = existing
        h.run()

    assert _status_updates(h) == [BulkUploadFileStatus.SKIPPED_DUPLICATE]
    h.job_repo.increment_duplicate_count.assert_called_once_with(h.job.id)
    h.decisions.log_duplicate_skipped.assert_called_once()
    assert h.decisions.log_duplicate_skipped.call_args.args[0] is existing
    h.pipeline.run.assert_not_called()
    h.resume_repo.create.assert_not_called()
    h.cc_service.create_campaign_candidate.assert_not_called()


def test_same_candidate_in_campaign_is_held_for_a_decision_and_nothing_changes():
    with _Harness() as h:
        known = SimpleNamespace(id=uuid4())
        existing = SimpleNamespace(id=uuid4(), campaign_id=h.campaign_id, candidate_id=known.id, resume_id=uuid4())
        h.candidate_repo.get_by_email_hash.return_value = known
        h.cc_repo.get_by_campaign_and_candidate.return_value = existing
        h.run()

    assert _status_updates(h) == [BulkUploadFileStatus.NEEDS_DECISION]
    h.job_repo.increment_needs_decision_count.assert_called_once_with(h.job.id)
    hold = h.decisions.hold_for_decision.call_args.kwargs
    assert hold["cc"] is existing and hold["job_file"] is h.job_file and hold["file_format"] == FileFormat.PDF
    # The known bug is fixed: no Resume version created or deactivated, no candidate touched.
    h.resume_repo.create.assert_not_called()
    h.resume_repo.deactivate_active_version.assert_not_called()
    h.candidate_service.get_or_create.assert_not_called()
    h.cc_service.create_campaign_candidate.assert_not_called()
    h.pipeline.run.assert_not_called()
    h.storage.delete_file.assert_not_called()
    h.job_repo.increment_failed_count.assert_not_called()
    h.job_repo.increment_processed_count.assert_not_called()


def test_known_candidate_new_campaign_gets_a_new_version_and_other_campaigns_are_stamped():
    with _Harness() as h:
        candidate = SimpleNamespace(id=uuid4())
        h.candidate_service.get_or_create.return_value = candidate
        h.resume_repo.get_active_by_candidate.return_value = SimpleNamespace(id=uuid4())
        h.resume_repo.get_max_version_number.return_value = 1
        other = SimpleNamespace(id=uuid4(), campaign_id=uuid4(), score_out_of_date_at=None,
                                score_out_of_date_reason=None)
        h.cc_repo.get_by_candidate_id.return_value = [other]
        h.run()

    resume = h.resume_repo.create.call_args.args[0]
    assert resume.version_number == 2 and resume.is_active_version is True
    h.cc_service.create_campaign_candidate.assert_called_once()
    assert other.score_out_of_date_at is not None and "Backend Engineer" in other.score_out_of_date_reason
    assert _status_updates(h) == [BulkUploadFileStatus.PROCESSED]


def test_new_candidate_stamps_nothing():
    with _Harness() as h:
        h.candidate_service.get_or_create.return_value = SimpleNamespace(id=uuid4())
        h.run()
    h.cc_repo.get_by_candidate_id.assert_not_called()
    assert _status_updates(h) == [BulkUploadFileStatus.PROCESSED]


def test_identical_file_only_in_another_campaign_still_links_the_existing_candidate():
    with _Harness() as h:
        matched = SimpleNamespace(id=uuid4(), candidate_id=uuid4())
        h.resume_repo.get_by_file_hash_global.return_value = matched
        h.candidate_repo.get_by_id.return_value = SimpleNamespace(id=matched.candidate_id)
        h.run()
    h.cc_service.create_campaign_candidate.assert_called_once()
    assert _status_updates(h) == [BulkUploadFileStatus.PROCESSED]
    h.job_repo.increment_duplicate_count.assert_called_once()


# --- finalisation and the file log -----------------------------------------------

def _job(**counts):
    base = dict(id=uuid4(), status=BulkUploadStatus.PROCESSING, total_files=3, processed_count=0, failed_count=0,
                duplicate_count=0, needs_decision_count=0)
    base.update(counts)
    return SimpleNamespace(**base)


def test_job_finalises_when_the_remaining_files_need_a_decision():
    from app.tasks.bulk_upload_tasks import _maybe_finalize_job
    job_repo = MagicMock()
    job_repo.get_by_id.return_value = _job(processed_count=1, duplicate_count=1, needs_decision_count=1)
    _maybe_finalize_job(job_repo, uuid4())
    assert job_repo.update_status.call_args.args[1] == BulkUploadStatus.COMPLETED


def test_job_with_failures_and_held_files_is_partial_failure():
    from app.tasks.bulk_upload_tasks import _maybe_finalize_job
    job_repo = MagicMock()
    job_repo.get_by_id.return_value = _job(failed_count=2, needs_decision_count=1)
    _maybe_finalize_job(job_repo, uuid4())
    assert job_repo.update_status.call_args.args[1] == BulkUploadStatus.PARTIAL_FAILURE


def test_job_not_finalised_while_files_are_still_running():
    from app.tasks.bulk_upload_tasks import _maybe_finalize_job
    job_repo = MagicMock()
    job_repo.get_by_id.return_value = _job(needs_decision_count=1)
    _maybe_finalize_job(job_repo, uuid4())
    job_repo.update_status.assert_not_called()


def test_file_log_shows_skipped_duplicate_and_needs_decision():
    from app.schemas.bulk_upload.response import BulkUploadFileLogResult
    from app.services.bulk_upload.bulk_upload_service import BulkUploadService
    service = BulkUploadService.__new__(BulkUploadService)
    job = SimpleNamespace(id=uuid4(), completed_at=None, created_at=datetime.now(timezone.utc))
    now = datetime.now(timezone.utc)
    skipped = SimpleNamespace(id=uuid4(), original_filename="a.pdf", status=BulkUploadFileStatus.SKIPPED_DUPLICATE,
                              task_id="t1", created_at=now)
    held = SimpleNamespace(id=uuid4(), original_filename="b.pdf", status=BulkUploadFileStatus.NEEDS_DECISION,
                           task_id="t2", created_at=now)
    log = SimpleNamespace(output_summary="Skipped - this exact file is already a candidate's resume in this campaign.",
                          error_message=None, completed_at=now)

    skipped_entry = service._to_file_log_entry(skipped, log, job)
    held_entry = service._to_file_log_entry(held, None, job)

    assert skipped_entry.result == BulkUploadFileLogResult.DUPLICATE
    assert skipped_entry.status == "SKIPPED_DUPLICATE" and "already" in skipped_entry.reason
    assert held_entry.result == BulkUploadFileLogResult.NEEDS_DECISION
    assert held_entry.status == "NEEDS_DECISION" and "decision" in held_entry.reason
    assert held_entry.file_id == held.id


def test_progress_counts_held_files_as_resolved():
    from app.services.bulk_upload.bulk_upload_service import BulkUploadService
    service = BulkUploadService.__new__(BulkUploadService)
    service.bulk_upload_job_repo = MagicMock()
    service.bulk_upload_job_repo.get_by_id.return_value = _job(status=BulkUploadStatus.COMPLETED, processed_count=2,
                                                               needs_decision_count=1)
    _job_row, percent, remaining, _eta = service.get_job_progress(uuid4())
    assert remaining == 0 and percent == 100.0
