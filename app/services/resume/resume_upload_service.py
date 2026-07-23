import hashlib
import logging
from uuid import UUID, uuid4

from app.core.storage_service import StorageService
from app.enums.constants import ActionType, EntityType
from app.exceptions.resume_exceptions import EncryptionUnavailableException
from app.exceptions.storage_exception import StorageException
from app.models.candidates import FileFormat, ParseStatus, Resume
from app.repositories.circuit_breaker_repository import CircuitBreakerRepository
from app.repositories.resume_repository import ResumeRepository
from app.services.audit_service import AuditService
from app.services.resume.candidate_service import CandidateService
from app.services.resume.file_validation_service import FileValidationService

logger = logging.getLogger(__name__)

_FORMAT_TO_EXTENSION = {
    FileFormat.PDF: "pdf",
    FileFormat.DOCX: "docx",
    FileFormat.PNG: "png",
    FileFormat.JPEG: "jpg",
}

STORAGE_SERVICE_NAME = "SUPABASE_STORAGE"
ENCRYPTION_SERVICE_NAME = "ENCRYPTION_SERVICE"


class ResumeUploadService:
    """
    Synchronous upload leg only: validate -> store file -> get-or-create
    candidate -> create the resume record at parse_status=PENDING. No
    Celery task is enqueued here — ResumeIntakeService does that, after
    this returns. No campaign-candidate pipeline record is created here
    either.

    Distinct from ResumeService (app/services/resume/resume_service.py),
    which owns the async pipeline's persistence stage (writing
    parsed_json/embeddings/skills for a Resume row that already exists) —
    the two were originally both named ResumeService on separate branches
    before this merge; this one was renamed to keep its own, narrower
    responsibility unambiguous.
    """

    RESUME_STORAGE_BUCKET = "airs_resumes"

    def __init__(
        self,
        resume_repo: ResumeRepository,
        candidate_service: CandidateService,
        file_validation_service: FileValidationService,
        storage_service: StorageService,
        circuit_breaker_repo: CircuitBreakerRepository,
        audit_service: AuditService,
    ):
        self.resume_repo = resume_repo
        self.candidate_service = candidate_service
        self.file_validation_service = file_validation_service
        self.storage_service = storage_service
        self.circuit_breaker_repo = circuit_breaker_repo
        self.audit_service = audit_service

    def upload(
        self,
        file_bytes: bytes,
        filename: str,
        candidate_full_name: str,
        candidate_email: str,
        jurisdiction: str,
        consent_source: str,
        uploaded_by: str,
        content_type: str | None = None,
        candidate_phone: str | None = None,
        org_id: UUID | None = None,
        source_campaign_id: UUID | None = None,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> Resume:
        validation_result = self.file_validation_service.validate(file_bytes, filename)
        file_hash = self._hash_file_bytes(file_bytes)

        object_path = self._build_object_path(org_id, validation_result.file_format)
        try:
            self.storage_service.upload_file(
                bucket_name=self.RESUME_STORAGE_BUCKET,
                file_path=object_path,
                file_content=file_bytes,
                content_type=content_type,
            )
        except StorageException:
            self._safe_record_infra_failure(STORAGE_SERVICE_NAME)
            raise

        try:
            candidate = self.candidate_service.get_or_create(
                full_name=candidate_full_name,
                email=candidate_email,
                jurisdiction=jurisdiction,
                consent_source=consent_source,
                phone=candidate_phone,
                org_id=org_id,
                source_campaign_id=source_campaign_id,
                ip_address=ip_address,
                user_agent=user_agent,
            )
        except EncryptionUnavailableException:
            self._safe_record_infra_failure(ENCRYPTION_SERVICE_NAME)
            raise

        resume = Resume(
            candidate_id=candidate.id,
            file_path=object_path,
            file_format=validation_result.file_format,
            file_hash=file_hash,
            version_number=1,
            is_active_version=True,
            parse_status=ParseStatus.PENDING,
            uploaded_by=uploaded_by,
        )

        try:
            resume = self.resume_repo.create(resume)
            self.resume_repo.commit()
        except Exception:
            self.resume_repo.rollback()
            raise

        return resume

    def record_task_id(self, resume: Resume, task_id: str) -> None:
        """
        Persists the processing task's id on the Resume row itself, at
        enqueue time — before Celery ever picks it up — so a future
        monitoring API can resolve resume_id -> task_id at any point in
        the resume's lifecycle, not just after the task first succeeds
        (the only time celery_task_log.resume_id gets set today).
        """
        self.resume_repo.set_task_id(resume, task_id)
        self.resume_repo.commit()

    def _build_object_path(self, org_id: UUID | None, file_format: FileFormat) -> str:
        extension = _FORMAT_TO_EXTENSION[file_format]
        return f"org_{org_id}/resume/{uuid4()}.{extension}"

    @staticmethod
    def _hash_file_bytes(file_bytes: bytes) -> str:
        return hashlib.md5(file_bytes).hexdigest()

    def _safe_record_infra_failure(self, service_name: str) -> None:
        """
        Records a circuit-breaker failure for service_name and audit-logs
        the CLOSED -> OPEN transition, if this call caused one. Deliberately
        swallows any error from this bookkeeping itself — a broken
        circuit-breaker write must never mask the real infra exception the
        caller is about to re-raise.
        """
        try:
            state, just_opened = self.circuit_breaker_repo.increment_failure(service_name)
            self.circuit_breaker_repo.commit()

            if just_opened:
                self.audit_service.log(
                    actor_id=None,
                    actor_role=None,
                    action_type=ActionType.CIRCUIT_BREAKER_OPENED,
                    entity_type=EntityType.CIRCUIT_BREAKER,
                    entity_id=state.id,
                    details={
                        "service_name": service_name,
                        "failure_count": state.failure_count,
                        "failure_threshold": state.failure_threshold,
                    },
                )
                self.circuit_breaker_repo.commit()
        except Exception:
            logger.exception(
                "Failed to record circuit-breaker failure for service '%s'.", service_name,
            )
            self.circuit_breaker_repo.rollback()
