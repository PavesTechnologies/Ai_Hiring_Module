import hashlib
import io
import logging
import zipfile
from datetime import datetime, timezone
from uuid import UUID, uuid4

from app.core.celery_app import celery_app
from app.core.encryption_service import EncryptionService
from app.core.storage_service import StorageService
from app.db.session import SessionLocal
from app.enums.constants import ActionType, EntityType, Jurisdiction
from app.exceptions.bulk_upload_exceptions import MaxFilesExceededException
from app.exceptions.campaign_exceptions import CampaignException
from app.models.async_tasks import (
    BulkUploadFileStatus,
    BulkUploadJobFile,
    BulkUploadStatus,
    DocumentType,
    ProcessingStage,
)
from app.models.candidates import FileFormat, ParseAttemptStatus, ParseStatus, Resume
from app.repositories.audit_repository import AuditRepository
from app.repositories.bulk_upload_job_file_repository import BulkUploadJobFileRepository
from app.repositories.bulk_upload_job_repository import BulkUploadJobRepository
from app.repositories.CampaignRepository import CampaignRepository
from app.repositories.campaign_candidate_repository import CampaignCandidateRepository
from app.repositories.candidate_repository import CandidateRepository
from app.repositories.celery_task_log_repository import CeleryTaskLogRepository
from app.repositories.checkpoint_repository import CheckpointRepository
from app.repositories.config_repository import ConfigRepository
from app.repositories.consent_repository import ConsentRepository
from app.repositories.dead_letter_queue_repository import DeadLetterQueueRepository
from app.repositories.encryption_key_repository import EncryptionKeyRepository
from app.repositories.resume_repository import ResumeRepository
from app.repositories.stage_failure_log_repository import StageFailureLogRepository
from app.schemas.campaign.campaign_candidate_schema import CampaignCandidateCreateRequest
from app.services.audit_service import AuditService
from app.services.ai.preprocessing_service import PreprocessingService
from app.services.campaign.campaign_candidate_service import CampaignCandidateService
from app.services.celery_task_log_service import CeleryTaskLogService
from app.services.compliance.consent_service import ConsentService
from app.services.document_processing.retry_driver import RetryDriver
from app.services.document_processing.stage_execution_service import StageExecutionError
from app.services.document_processing.text_extraction_service import TextExtractionService
from app.services.extractions.gemini_resume_extraction_service import GeminiResumeExtractionService
from app.services.bulk_upload.zip_validation_service import ZipValidationService
from app.services.resume.candidate_service import CandidateService
from app.services.resume.file_validation_service import FileValidationService

logger = logging.getLogger(__name__)

BULK_UPLOAD_STORAGE_BUCKET = "airs_resumes"
BULK_UPLOAD_CONSENT_SOURCE = "BULK_UPLOAD_FORM"
BULK_RESUME_PARSE_TASK_TYPE = "BULK_RESUME_PARSE"
PARSER_NAME = "gemini-resume-parser"
PARSER_VERSION = "v1"
_JUNK_PATH_PREFIXES = ("__MACOSX/",)
_IMAGE_FORMATS = (FileFormat.PNG, FileFormat.JPEG)


def _run_stage(checkpoint_repo, task_id: str, stage: ProcessingStage, context_data: dict, fn):
    """
    Lightweight counterpart to StageExecutionService.run_stage — tags a
    failure with its ProcessingStage and saves just enough checkpoint
    context for RetryDriver's dead-letter entry to be inspectable, without
    adopting per-stage DocumentProcessingStageExecution row tracking (no
    per-file status-polling endpoint exists for bulk uploads to show it).
    """
    try:
        return fn()
    except Exception as exc:
        checkpoint_repo.upsert(
            task_id, DocumentType.RESUME, failed_at_stage=stage, context_data=context_data,
        )
        checkpoint_repo.commit()
        raise StageExecutionError(stage, exc) from exc


@celery_app.task(name="bulk_upload.extract_zip")
def extract_bulk_upload_zip(task_id: str, bulk_upload_job_id: str) -> None:
    """
    BULK_EXTRACT: downloads the bulk_upload_jobs' stored ZIP, enumerates its
    real entries and rejects the whole job outright if it exceeds
    MAX_FILES_PER_ZIP (Phase B5), otherwise stages each real file entry as
    its own object in storage, records one bulk_upload_job_files row per
    file (status=QUEUED), and enqueues Phase B4's per-file parse task for
    each. Does not parse or validate any file's content itself — that
    happens per file in BULK_RESUME_PARSE.
    """
    db = SessionLocal()
    task_log = None
    job = None
    uploaded_paths: list[str] = []
    try:
        job_repo = BulkUploadJobRepository(db)
        file_repo = BulkUploadJobFileRepository(db)
        config_repo = ConfigRepository(db)
        task_log_repo = CeleryTaskLogRepository(db)
        task_log_service = CeleryTaskLogService(task_log_repo)
        storage_service = StorageService()
        zip_validation_service = ZipValidationService(config_repo)

        task_log = task_log_service.create_log(task_id=task_id, task_type="BULK_EXTRACT")
        task_log.bulk_upload_job_id = UUID(bulk_upload_job_id)
        task_log_repo.update(task_log)
        task_log_repo.commit()

        job = job_repo.get_by_id(UUID(bulk_upload_job_id))
        if job is None:
            raise ValueError(f"bulk_upload_jobs row {bulk_upload_job_id} not found.")

        job_repo.update_status(job.id, BulkUploadStatus.EXTRACTING)
        job_repo.commit()

        zip_bytes = storage_service.download_file(BULK_UPLOAD_STORAGE_BUCKET, job.zip_storage_path)

        try:
            archive = zipfile.ZipFile(io.BytesIO(zip_bytes))
        except zipfile.BadZipFile as exc:
            job_repo.update_status(
                job.id, BulkUploadStatus.FAILED, error_summary=f"Corrupt ZIP archive: {exc}",
            )
            job_repo.commit()
            task_log_service.mark_failure(task_log, f"Corrupt ZIP archive: {exc}")
            return

        real_entries: list[tuple[object, str]] = []
        for entry in archive.infolist():
            if entry.is_dir():
                continue
            if any(entry.filename.startswith(prefix) for prefix in _JUNK_PATH_PREFIXES):
                continue
            basename = entry.filename.rsplit("/", 1)[-1]
            if not basename or basename.startswith("."):
                continue
            real_entries.append((entry, basename))

        try:
            zip_validation_service.validate_file_count(len(real_entries))
        except MaxFilesExceededException as exc:
            job_repo.update_status(job.id, BulkUploadStatus.FAILED, error_summary=exc.message)
            job_repo.commit()
            task_log_service.mark_failure(task_log, exc.message)
            return

        staged_files: list[BulkUploadJobFile] = []
        for entry, basename in real_entries:
            file_bytes = archive.read(entry)
            object_path = f"campaign_{job.campaign_id}/bulk-zip/{job.id}/{uuid4()}_{basename}"
            storage_service.upload_file(
                bucket_name=BULK_UPLOAD_STORAGE_BUCKET,
                file_path=object_path,
                file_content=file_bytes,
            )
            uploaded_paths.append(object_path)
            staged_files.append(
                BulkUploadJobFile(
                    bulk_upload_job_id=job.id,
                    original_filename=basename,
                    storage_path=object_path,
                    status=BulkUploadFileStatus.QUEUED,
                )
            )

        if staged_files:
            staged_files = file_repo.create_many(staged_files)
            file_repo.commit()

        job_repo.set_total_files(job.id, len(staged_files))
        job_repo.update_status(
            job.id,
            BulkUploadStatus.PROCESSING if staged_files else BulkUploadStatus.FAILED,
            error_summary=None if staged_files else "ZIP archive contained no valid files.",
        )
        if staged_files:
            job_repo.increment_queued_count(job.id, by=len(staged_files))
        job_repo.commit()

        for staged_file in staged_files:
            per_file_task_id = uuid4()
            parse_bulk_upload_file.apply_async(
                kwargs={
                    "task_id": str(per_file_task_id),
                    "bulk_upload_job_file_id": str(staged_file.id),
                },
                task_id=str(per_file_task_id),
            )

        summary = f"Extracted {len(staged_files)} file(s) from '{job.original_filename}'."
        task_log_service.mark_success(task_log, summary=summary)

    except Exception as ex:
        if task_log:
            task_log_service.mark_failure(task_log, str(ex))
        if job is not None:
            # Phase B9: previously only the task_log reflected this failure —
            # the job itself stayed stuck at EXTRACTING forever (invisible as
            # "failed" to the history/detail endpoints), and any files
            # already uploaded before the failure were left as storage
            # orphans with no bulk_upload_job_files row ever pointing at them.
            _cleanup_orphaned_uploads(storage_service, uploaded_paths)
            job_repo.update_status(job.id, BulkUploadStatus.FAILED, error_summary=str(ex))
            job_repo.commit()
        logger.exception("Bulk upload extraction task failed for job_id %s", bulk_upload_job_id)
        raise

    finally:
        db.close()


def _cleanup_orphaned_uploads(storage_service: StorageService, paths: list[str]) -> None:
    """
    Best-effort delete of files already uploaded to storage before an
    extraction failure aborted the run partway through — never let a
    cleanup failure mask the real exception the caller is about to re-raise.
    """
    for path in paths:
        try:
            storage_service.delete_file(BULK_UPLOAD_STORAGE_BUCKET, path)
        except Exception:
            logger.warning("Failed to clean up orphaned upload '%s' after extraction failure.", path)


@celery_app.task(name="bulk_upload.parse_file", bind=True)
def parse_bulk_upload_file(self, task_id: str, bulk_upload_job_file_id: str) -> None:
    """
    BULK_RESUME_PARSE: the "parse-first" per-file leg of a bulk upload.
    Unlike the individual-upload pipeline (which parses a Resume that
    already has a Candidate attached), no candidate identity exists yet
    here — text/AI extraction runs first to learn the candidate's
    name/email/phone from the file itself, and only then are
    Candidate/Resume/CampaignCandidate created.

    Transient-prone steps (download, text extraction/cleaning, AI
    extraction) are retried with backoff via the same RetryDriver/DLQ
    infrastructure the JD pipeline already uses (Phase B6) — a bad Gemini
    response gets retried rather than immediately failing the file.
    Deterministic outcomes (corrupt/unsupported file, no identifiable
    candidate, duplicate candidate already in the campaign) are recorded
    immediately, unchanged from Phase B4 — retrying those could never
    succeed differently. Either way, a single file's outcome is recorded
    on the job's counters and does not affect any other file's processing.
    """
    db = SessionLocal()
    task_log = None
    job_file = None
    job = None
    retry_driver = None
    attempt_number = 1
    try:
        file_repo = BulkUploadJobFileRepository(db)
        job_repo = BulkUploadJobRepository(db)
        config_repo = ConfigRepository(db)
        candidate_repo = CandidateRepository(db)
        resume_repo = ResumeRepository(db)
        encryption_key_repo = EncryptionKeyRepository(db)
        consent_repo = ConsentRepository(db)
        campaign_repo = CampaignRepository(db)
        campaign_candidate_repo = CampaignCandidateRepository(db)
        audit_repo = AuditRepository(db)
        task_log_repo = CeleryTaskLogRepository(db)
        checkpoint_repo = CheckpointRepository(db)
        stage_failure_log_repo = StageFailureLogRepository(db)
        dead_letter_queue_repo = DeadLetterQueueRepository(db)

        encryption_service = EncryptionService(encryption_key_repo)
        consent_service = ConsentService(consent_repo, config_repo)
        candidate_service = CandidateService(candidate_repo, encryption_service, consent_service)
        file_validation_service = FileValidationService(config_repo)
        audit_service = AuditService(audit_repo)
        campaign_candidate_service = CampaignCandidateService(campaign_repo, campaign_candidate_repo, audit_service)
        extraction_service = GeminiResumeExtractionService()
        preprocessing_service = PreprocessingService()
        storage_service = StorageService()
        task_log_service = CeleryTaskLogService(task_log_repo)

        existing_task_log = task_log_repo.get_by_task_id(task_id)
        if existing_task_log is None:
            task_log = task_log_service.create_log(task_id=task_id, task_type=BULK_RESUME_PARSE_TASK_TYPE)
        else:
            task_log = task_log_service.mark_running(existing_task_log)

        job_file = file_repo.get_by_id(UUID(bulk_upload_job_file_id))
        if job_file is None:
            raise ValueError(f"bulk_upload_job_files row {bulk_upload_job_file_id} not found.")

        task_log.bulk_upload_job_id = job_file.bulk_upload_job_id
        task_log_repo.update(task_log)
        task_log_repo.commit()

        if not file_repo.try_start_processing(job_file.id):
            # Phase B7: atomically claims QUEUED -> RUNNING; False means the
            # file was no longer QUEUED — almost always because it was
            # bulk-cancelled — so skip it without touching any job counters
            # (a cancelled file is neither processed, failed, nor duplicate;
            # it's simply excluded). Also closes the race where this task
            # started a moment before the cancel request arrived.
            file_repo.commit()
            task_log_service.mark_paused(task_log)
            logger.info(
                "Bulk upload file %s skipped — its job was cancelled.", bulk_upload_job_file_id,
            )
            return
        file_repo.commit()

        job = job_repo.get_by_id(job_file.bulk_upload_job_id)
        if job is None:
            raise ValueError(f"bulk_upload_jobs row {job_file.bulk_upload_job_id} not found.")

        retry_driver = RetryDriver(
            checkpoint_repo,
            stage_failure_log_repo,
            dead_letter_queue_repo,
            task_log_service,
            task_log,
            task_type=BULK_RESUME_PARSE_TASK_TYPE,
        )
        attempt_number = self.request.retries + 1
        context_data = {
            "bulk_upload_job_file_id": str(job_file.id),
            "bulk_upload_job_id": str(job.id),
            "storage_path": job_file.storage_path,
            "original_filename": job_file.original_filename,
        }

        file_bytes = _run_stage(
            checkpoint_repo, task_id, ProcessingStage.STORAGE, context_data,
            lambda: storage_service.download_file(BULK_UPLOAD_STORAGE_BUCKET, job_file.storage_path),
        )

        validation_result = file_validation_service.validate(file_bytes, job_file.original_filename)

        if validation_result.file_format in _IMAGE_FORMATS:
            raise ValueError(
                "Image-format resumes (PNG/JPEG) require OCR, which is not yet implemented."
            )

        text = _run_stage(
            checkpoint_repo, task_id, ProcessingStage.TEXT_EXTRACTION, context_data,
            lambda: TextExtractionService.extract_for_resume(file_bytes, validation_result.file_format),
        )
        cleaned_text = _run_stage(
            checkpoint_repo, task_id, ProcessingStage.TEXT_CLEANING, context_data,
            lambda: preprocessing_service.normalize(text),
        )
        extracted = _run_stage(
            checkpoint_repo, task_id, ProcessingStage.AI_EXTRACTION, context_data,
            lambda: extraction_service.extract(cleaned_text),
        )

        if not extracted.full_name or not extracted.email:
            raise ValueError(
                "Could not identify a candidate name and email from this resume."
            )

        candidate = candidate_service.get_or_create(
            full_name=extracted.full_name,
            email=extracted.email,
            jurisdiction=Jurisdiction.GLOBAL.value,
            consent_source=BULK_UPLOAD_CONSENT_SOURCE,
            phone=extracted.phone,
            source_campaign_id=job.campaign_id,
        )

        page_count = (
            TextExtractionService.get_pdf_page_count(file_bytes)
            if validation_result.file_format == FileFormat.PDF
            else None
        )

        resume = Resume(
            candidate_id=candidate.id,
            file_path=job_file.storage_path,
            file_format=validation_result.file_format,
            file_hash=hashlib.md5(file_bytes).hexdigest(),
            version_number=1,
            is_active_version=True,
            parsed_json=extracted.model_dump(),
            parse_status=ParseStatus.PARSED,
            parser_version=f"{PARSER_NAME}-{PARSER_VERSION}",
            page_count=page_count,
            ocr_used=False,
            uploaded_by=job.uploaded_by,
            bulk_upload_job_id=job.id,
        )
        resume = resume_repo.create(resume)
        resume_repo.record_parse_attempt(
            resume_id=resume.id,
            attempt_number=1,
            parser_used=PARSER_NAME,
            parser_version=PARSER_VERSION,
            status=ParseAttemptStatus.SUCCESS,
            confidence_score=1.0,
        )
        resume_repo.commit()

        campaign_candidate_service.create_campaign_candidate(
            CampaignCandidateCreateRequest(
                campaign_id=job.campaign_id,
                candidate_id=candidate.id,
                resume_id=resume.id,
            ),
            actor_id=job.uploaded_by,
        )

        audit_service.log(
            actor_id=job.uploaded_by,
            actor_role=None,
            action_type=ActionType.RESUME_UPLOADED,
            entity_type=EntityType.RESUME,
            entity_id=resume.id,
            campaign_id=job.campaign_id,
            details={
                "candidate_id": str(candidate.id),
                "file_format": resume.file_format.value,
                "bulk_upload_job_id": str(job.id),
            },
        )

        active_checkpoint = checkpoint_repo.get(task_id)
        if active_checkpoint is not None:
            checkpoint_repo.delete(task_id)
            checkpoint_repo.commit()

        file_repo.update_status(job_file.id, BulkUploadFileStatus.PROCESSED)
        job_repo.increment_processed_count(job.id)
        job_repo.commit()
        _maybe_finalize_job(job_repo, job.id)

        task_log_service.mark_success(
            task_log, summary=f"Parsed '{job_file.original_filename}' -> candidate {candidate.id}.",
        )

    except StageExecutionError as stage_exc:
        should_retry = False
        if retry_driver is not None:
            should_retry = retry_driver.handle_failure(
                self, task_id, DocumentType.RESUME, stage_exc, attempt_number,
            )
        if not should_retry:
            if job_file is not None and job is not None:
                file_repo.update_status(job_file.id, BulkUploadFileStatus.FAILED)
                job_repo.increment_failed_count(job.id)
                job_repo.commit()
                _maybe_finalize_job(job_repo, job.id)
            if task_log:
                task_log_service.mark_failure(task_log, str(stage_exc.original))
            logger.warning(
                "Bulk upload file %s permanently failed at stage %s: %s",
                bulk_upload_job_file_id, stage_exc.stage, stage_exc.original,
            )
            raise stage_exc.original

    except CampaignException as exc:
        is_duplicate = exc.status_code == 409 and "already exists in this campaign" in exc.message
        if job_file is not None and job is not None:
            file_repo.update_status(job_file.id, BulkUploadFileStatus.FAILED)
            if is_duplicate:
                job_repo.increment_duplicate_count(job.id)
            else:
                job_repo.increment_failed_count(job.id)
            job_repo.commit()
            _maybe_finalize_job(job_repo, job.id)
        if task_log:
            task_log_service.mark_failure(task_log, exc.message)

    except Exception as exc:
        if job_file is not None and job is not None:
            file_repo.update_status(job_file.id, BulkUploadFileStatus.FAILED)
            job_repo.increment_failed_count(job.id)
            job_repo.commit()
            _maybe_finalize_job(job_repo, job.id)
        if task_log:
            task_log_service.mark_failure(task_log, str(exc))
        logger.warning(
            "Bulk upload file %s failed to parse: %s", bulk_upload_job_file_id, exc,
        )

    finally:
        db.close()


def _maybe_finalize_job(job_repo: BulkUploadJobRepository, job_id: UUID) -> None:
    """
    Once every staged file has resolved (processed + failed + duplicate ==
    total_files), transitions the job out of PROCESSING into its terminal
    state. Idempotent — safe to call after every file resolves even though
    only the last one to finish actually changes anything.

    Only acts while the job is still PROCESSING (Phase B7): a file that was
    already running when the job got cancelled can still finish normally
    afterward, and must not flip an already-CANCELLED job back to
    COMPLETED/PARTIAL_FAILURE/FAILED.
    """
    job = job_repo.get_by_id(job_id)
    if job is None or job.status != BulkUploadStatus.PROCESSING:
        return

    if job.total_files == 0:
        return

    resolved = job.processed_count + job.failed_count + job.duplicate_count
    if resolved < job.total_files:
        return

    if job.failed_count == 0 and job.duplicate_count == 0:
        status = BulkUploadStatus.COMPLETED
    elif job.processed_count == 0:
        status = BulkUploadStatus.FAILED
    else:
        status = BulkUploadStatus.PARTIAL_FAILURE

    job_repo.update_status(job_id, status, completed_at=datetime.now(timezone.utc))
    job_repo.commit()
