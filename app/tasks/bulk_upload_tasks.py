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
from app.exceptions.campaign_exceptions import CampaignException
from app.models.async_tasks import BulkUploadFileStatus, BulkUploadJobFile, BulkUploadStatus
from app.models.candidates import FileFormat, ParseAttemptStatus, ParseStatus, Resume
from app.repositories.audit_repository import AuditRepository
from app.repositories.bulk_upload_job_file_repository import BulkUploadJobFileRepository
from app.repositories.bulk_upload_job_repository import BulkUploadJobRepository
from app.repositories.CampaignRepository import CampaignRepository
from app.repositories.campaign_candidate_repository import CampaignCandidateRepository
from app.repositories.candidate_repository import CandidateRepository
from app.repositories.celery_task_log_repository import CeleryTaskLogRepository
from app.repositories.config_repository import ConfigRepository
from app.repositories.consent_repository import ConsentRepository
from app.repositories.encryption_key_repository import EncryptionKeyRepository
from app.repositories.resume_repository import ResumeRepository
from app.schemas.campaign.campaign_candidate_schema import CampaignCandidateCreateRequest
from app.services.audit_service import AuditService
from app.services.ai.preprocessing_service import PreprocessingService
from app.services.campaign.campaign_candidate_service import CampaignCandidateService
from app.services.celery_task_log_service import CeleryTaskLogService
from app.services.compliance.consent_service import ConsentService
from app.services.document_processing.text_extraction_service import TextExtractionService
from app.services.extractions.gemini_resume_extraction_service import GeminiResumeExtractionService
from app.services.resume.candidate_service import CandidateService
from app.services.resume.file_validation_service import FileValidationService

logger = logging.getLogger(__name__)

BULK_UPLOAD_STORAGE_BUCKET = "airs_resumes"
BULK_UPLOAD_CONSENT_SOURCE = "BULK_UPLOAD_FORM"
PARSER_NAME = "gemini-resume-parser"
PARSER_VERSION = "v1"
_JUNK_PATH_PREFIXES = ("__MACOSX/",)
_IMAGE_FORMATS = (FileFormat.PNG, FileFormat.JPEG)


@celery_app.task(name="bulk_upload.extract_zip")
def extract_bulk_upload_zip(task_id: str, bulk_upload_job_id: str) -> None:
    """
    BULK_EXTRACT: downloads the bulk_upload_jobs' stored ZIP, stages each
    real file entry as its own object in storage, records one
    bulk_upload_job_files row per file (status=QUEUED), and enqueues
    Phase B4's per-file parse task for each. Does not parse or validate
    any file's content itself — that happens per file in BULK_RESUME_PARSE.
    """
    db = SessionLocal()
    task_log = None
    try:
        job_repo = BulkUploadJobRepository(db)
        file_repo = BulkUploadJobFileRepository(db)
        task_log_repo = CeleryTaskLogRepository(db)
        task_log_service = CeleryTaskLogService(task_log_repo)
        storage_service = StorageService()

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

        staged_files: list[BulkUploadJobFile] = []
        for entry in archive.infolist():
            if entry.is_dir():
                continue
            if any(entry.filename.startswith(prefix) for prefix in _JUNK_PATH_PREFIXES):
                continue
            basename = entry.filename.rsplit("/", 1)[-1]
            if not basename or basename.startswith("."):
                continue

            file_bytes = archive.read(entry)
            object_path = f"campaign_{job.campaign_id}/bulk-zip/{job.id}/{uuid4()}_{basename}"
            storage_service.upload_file(
                bucket_name=BULK_UPLOAD_STORAGE_BUCKET,
                file_path=object_path,
                file_content=file_bytes,
            )
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
        logger.exception("Bulk upload extraction task failed for job_id %s", bulk_upload_job_id)
        raise

    finally:
        db.close()


@celery_app.task(name="bulk_upload.parse_file")
def parse_bulk_upload_file(task_id: str, bulk_upload_job_file_id: str) -> None:
    """
    BULK_RESUME_PARSE: the "parse-first" per-file leg of a bulk upload.
    Unlike the individual-upload pipeline (which parses a Resume that
    already has a Candidate attached), no candidate identity exists yet
    here — text/AI extraction runs first to learn the candidate's
    name/email/phone from the file itself, and only then are
    Candidate/Resume/CampaignCandidate created. A single file's failure
    is recorded on the job's counters and does not raise — one bad
    resume in a ZIP must not affect any other file's processing.
    """
    db = SessionLocal()
    task_log = None
    job_file = None
    job = None
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

        task_log = task_log_service.create_log(task_id=task_id, task_type="BULK_RESUME_PARSE")

        job_file = file_repo.get_by_id(UUID(bulk_upload_job_file_id))
        if job_file is None:
            raise ValueError(f"bulk_upload_job_files row {bulk_upload_job_file_id} not found.")

        task_log.bulk_upload_job_id = job_file.bulk_upload_job_id
        task_log_repo.update(task_log)
        task_log_repo.commit()

        job = job_repo.get_by_id(job_file.bulk_upload_job_id)
        if job is None:
            raise ValueError(f"bulk_upload_jobs row {job_file.bulk_upload_job_id} not found.")

        try:
            file_bytes = storage_service.download_file(BULK_UPLOAD_STORAGE_BUCKET, job_file.storage_path)

            validation_result = file_validation_service.validate(file_bytes, job_file.original_filename)

            if validation_result.file_format in _IMAGE_FORMATS:
                raise ValueError(
                    "Image-format resumes (PNG/JPEG) require OCR, which is not yet implemented."
                )

            text = TextExtractionService.extract_for_resume(file_bytes, validation_result.file_format)
            cleaned_text = preprocessing_service.normalize(text)
            extracted = extraction_service.extract(cleaned_text)

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

            file_repo.update_status(job_file.id, BulkUploadFileStatus.PROCESSED)
            job_repo.increment_processed_count(job.id)
            job_repo.commit()
            _maybe_finalize_job(job_repo, job.id)

            task_log_service.mark_success(
                task_log, summary=f"Parsed '{job_file.original_filename}' -> candidate {candidate.id}.",
            )

        except CampaignException as exc:
            is_duplicate = exc.status_code == 409 and "already exists in this campaign" in exc.message
            file_repo.update_status(job_file.id, BulkUploadFileStatus.FAILED)
            if is_duplicate:
                job_repo.increment_duplicate_count(job.id)
            else:
                job_repo.increment_failed_count(job.id)
            job_repo.commit()
            _maybe_finalize_job(job_repo, job.id)
            task_log_service.mark_failure(task_log, exc.message)

        except Exception as exc:
            file_repo.update_status(job_file.id, BulkUploadFileStatus.FAILED)
            job_repo.increment_failed_count(job.id)
            job_repo.commit()
            _maybe_finalize_job(job_repo, job.id)
            task_log_service.mark_failure(task_log, str(exc))
            logger.warning(
                "Bulk upload file %s failed to parse: %s", bulk_upload_job_file_id, exc,
            )

    except Exception as ex:
        if task_log:
            task_log_service.mark_failure(task_log, str(ex))
        logger.exception(
            "Bulk upload parse task failed for job_file_id %s", bulk_upload_job_file_id,
        )
        raise

    finally:
        db.close()


def _maybe_finalize_job(job_repo: BulkUploadJobRepository, job_id: UUID) -> None:
    """
    Once every staged file has resolved (processed + failed + duplicate ==
    total_files), transitions the job out of PROCESSING into its terminal
    state. Idempotent — safe to call after every file resolves even though
    only the last one to finish actually changes anything.
    """
    counts = job_repo.get_counts(job_id)
    if counts is None:
        return

    total_files, processed_count, failed_count, duplicate_count = counts
    if total_files == 0:
        return

    resolved = processed_count + failed_count + duplicate_count
    if resolved < total_files:
        return

    if failed_count == 0 and duplicate_count == 0:
        status = BulkUploadStatus.COMPLETED
    elif processed_count == 0:
        status = BulkUploadStatus.FAILED
    else:
        status = BulkUploadStatus.PARTIAL_FAILURE

    job_repo.update_status(job_id, status, completed_at=datetime.now(timezone.utc))
    job_repo.commit()
