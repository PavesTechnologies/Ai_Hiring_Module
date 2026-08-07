import hashlib
import logging
from datetime import datetime, timezone
from uuid import UUID, uuid4

from app.core.encryption_service import EncryptionService
from app.core.storage_service import StorageService
from app.enums.constants import ActionType, EntityType
from app.exception_handler.exceptions import NotFoundError
from app.exceptions.resume_exceptions import (
    DeadLetterEntryNotReplayableException,
    DuplicateResumeFileException,
    EncryptionUnavailableException,
    ResumeNotRetryableException,
)
from app.exceptions.storage_exception import StorageException
from app.models.candidates import Candidate, FileFormat, ParseStatus, Resume
from app.repositories.campaign_candidate_repository import CampaignCandidateRepository
from app.repositories.CampaignRepository import CampaignRepository
from app.repositories.candidate_repository import CandidateRepository
from app.repositories.circuit_breaker_repository import CircuitBreakerRepository
from app.repositories.dead_letter_queue_repository import DeadLetterQueueRepository
from app.repositories.resume_repository import ResumeRepository
from app.schemas.resume.response import DuplicateFileWarningResponse
from app.services.audit_service import AuditService
from app.services.resume.candidate_service import CandidateService
from app.services.resume.file_validation_service import FileValidationService
from app.services.resume.upload_resume_result import UploadResumeResult
from app.tasks.resume_processing_tasks import process_resume_document

_AVAILABLE_RESOLUTIONS = ["use_existing", "upload_anyway"]

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
        candidate_repo: CandidateRepository,
        campaign_candidate_repo: CampaignCandidateRepository,
        encryption_service: EncryptionService,
        dead_letter_queue_repo: DeadLetterQueueRepository | None = None,
        campaign_repo: CampaignRepository | None = None,
    ):
        self.resume_repo = resume_repo
        self.candidate_service = candidate_service
        self.file_validation_service = file_validation_service
        self.storage_service = storage_service
        self.circuit_breaker_repo = circuit_breaker_repo
        self.audit_service = audit_service
        self.candidate_repo = candidate_repo
        self.campaign_candidate_repo = campaign_candidate_repo
        self.encryption_service = encryption_service
        # Epic 4 (M05-E04) Phase D10 - optional, additive: every existing
        # caller of this constructor omits these two and is unaffected;
        # only retry_parse/replay_from_dlq need them.
        self.dead_letter_queue_repo = dead_letter_queue_repo
        self.campaign_repo = campaign_repo

    def upload(
        self,
        file_bytes: bytes,
        filename: str,
        candidate_full_name: str,
        candidate_email: str,
        jurisdiction: str,
        consent_source: str,
        uploaded_by: str,
        actor_role: str | None = None,
        content_type: str | None = None,
        candidate_phone: str | None = None,
        org_id: UUID | None = None,
        source_campaign_id: UUID | None = None,
        ip_address: str | None = None,
        user_agent: str | None = None,
        resolution: str | None = None,
    ) -> UploadResumeResult:
        validation_result = self.file_validation_service.validate(file_bytes, filename)
        file_hash = self._hash_file_bytes(file_bytes)

        # Epic 3 (M05-E03) Phase C2 — exact-duplicate check, before any
        # storage write or candidate creation. System-wide match, so the
        # matched candidate may not be the one named in this request's form
        # fields at all.
        matched_resume = self.resume_repo.get_by_file_hash_global(file_hash)
        if matched_resume is not None:
            matched_candidate = self.candidate_repo.get_by_id(matched_resume.candidate_id)

            if resolution is None:
                raise DuplicateResumeFileException(
                    message="An identical resume file already exists in the system.",
                    data=self._build_duplicate_warning(matched_resume, matched_candidate),
                )

            if resolution == "use_existing":
                return UploadResumeResult(
                    resume=matched_resume,
                    requires_processing=False,
                    duplicate_found=True,
                    matched_resume=matched_resume,
                    matched_candidate=matched_candidate,
                )

            # resolution == "upload_anyway" — identity comes from the
            # matched file's candidate, not the form's name/email.
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

            resume = self._create_next_version(
                candidate=matched_candidate,
                object_path=object_path,
                validation_result=validation_result,
                file_hash=file_hash,
                uploaded_by=uploaded_by,
                original_filename=filename,
                file_size_bytes=len(file_bytes),
            )
            return UploadResumeResult(
                resume=resume,
                requires_processing=True,
                duplicate_found=True,
                matched_resume=matched_resume,
                matched_candidate=matched_candidate,
            )

        # No duplicate — existing flow, unchanged: storage upload happens
        # before candidate creation so a storage failure never leaves an
        # orphaned candidate row behind (get_or_create commits internally).
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
                actor_id=uploaded_by,
                actor_role=actor_role,
                phone=candidate_phone,
                org_id=org_id,
                source_campaign_id=source_campaign_id,
                ip_address=ip_address,
                user_agent=user_agent,
            )
        except EncryptionUnavailableException:
            self._safe_record_infra_failure(ENCRYPTION_SERVICE_NAME)
            raise

        resume = self._create_next_version(
            candidate=candidate,
            object_path=object_path,
            validation_result=validation_result,
            file_hash=file_hash,
            uploaded_by=uploaded_by,
            original_filename=filename,
            file_size_bytes=len(file_bytes),
        )
        return UploadResumeResult(resume=resume, requires_processing=True, duplicate_found=False)

    def _create_next_version(
        self,
        candidate: Candidate,
        object_path: str,
        validation_result,
        file_hash: str,
        uploaded_by: str,
        original_filename: str,
        file_size_bytes: int,
    ) -> Resume:
        """
        Epic 3 (M05-E03) Phase C1 logic, factored out so both the no-duplicate
        path and C2's "upload_anyway" path share it: an active resume already
        existing for this candidate is what distinguishes a resubmission from
        a brand-new candidate's first upload.
        """
        existing_active = self.resume_repo.get_active_by_candidate(candidate.id)
        if existing_active is None:
            version_number = 1
        else:
            version_number = self.resume_repo.get_max_version_number(candidate.id) + 1
            self.resume_repo.deactivate_active_version(candidate.id)

        resume = Resume(
            candidate_id=candidate.id,
            file_path=object_path,
            file_format=validation_result.file_format,
            file_hash=file_hash,
            original_filename=original_filename,
            file_size_bytes=file_size_bytes,
            version_number=version_number,
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

    def _build_duplicate_warning(
        self,
        matched_resume: Resume,
        matched_candidate: Candidate,
    ) -> DuplicateFileWarningResponse:
        candidate_name = self.encryption_service.decrypt(
            matched_candidate.full_name_encrypted, matched_candidate.encryption_key_id,
        )
        campaign_context = self.campaign_candidate_repo.get_campaign_context_for_candidate(matched_candidate.id)

        return DuplicateFileWarningResponse(
            duplicate_resume_id=matched_resume.id,
            candidate_id=matched_candidate.id,
            candidate_name=candidate_name,
            uploaded_at=matched_resume.created_at,
            current_pipeline_stage=campaign_context[0][1].value if campaign_context else None,
            campaign_names=[name for name, _ in campaign_context],
            original_filename=matched_resume.original_filename,
            available_resolutions=list(_AVAILABLE_RESOLUTIONS),
        )

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

    def retry_parse(
        self,
        resume_id: UUID,
        actor_id: str,
        actor_role: str | None = None,
    ) -> tuple[Resume, UUID]:
        """
        Epic 4 (M05-E04) Phase D10 - re-dispatches a FAILED resume's
        processing from its existing, already-stored file_path (no new
        upload). Mirrors BulkUploadService.replay_failed_file's shape:
        domain-state flip + audit log committed atomically, dispatch
        happens after commit so a Celery-dispatch failure never rolls
        back the already-committed status flip.
        """
        resume = self.resume_repo.get_by_id(resume_id)
        if resume is None:
            raise NotFoundError(f"Resume {resume_id} not found.")
        if resume.parse_status != ParseStatus.FAILED:
            raise ResumeNotRetryableException(
                f"Only a FAILED resume can be retried (current status: {resume.parse_status.value})."
            )

        campaign = self._resolve_owning_campaign(resume_id)
        if campaign is None:
            raise ResumeNotRetryableException(
                "This resume has no linked campaign, so its prompt template cannot be resolved for a retry."
            )
        new_task_id = uuid4()

        try:
            self.resume_repo.mark_parse_pending(resume)
            self.resume_repo.set_task_id(resume, str(new_task_id))
            self.audit_service.log(
                actor_id=actor_id,
                actor_role=actor_role,
                action_type=ActionType.RESUME_UPLOAD_RETRIED,
                entity_type=EntityType.RESUME,
                entity_id=resume.id,
                campaign_id=campaign.id,
                details={"new_task_id": str(new_task_id)},
            )
            self.resume_repo.commit()
        except Exception:
            self.resume_repo.rollback()
            raise

        process_resume_document.apply_async(
            kwargs={
                "resume_id": str(resume.id),
                "prompt_template_id": str(campaign.prompt_template_id),
            },
            task_id=str(new_task_id),
        )
        return resume, new_task_id

    def replay_from_dlq(
        self,
        dlq_id: UUID,
        actor_id: str,
        actor_role: str | None = None,
    ) -> tuple[Resume, UUID]:
        """
        Epic 4 (M05-E04) Phase D10 - replays a dead-lettered resume-parse
        failure from its DLQ entry. input_payload is never read (it's
        always None for resumes - ResumeProcessingPipeline never writes
        checkpoints); resume_id (populated by the RetryDriver fix this
        same phase made) is the actual reconstruction path.
        """
        if self.dead_letter_queue_repo is None:
            raise RuntimeError("dead_letter_queue_repo was not configured on this service.")

        dlq_entry = self.dead_letter_queue_repo.get_by_id(dlq_id)
        if dlq_entry is None:
            raise NotFoundError(f"Dead letter queue entry {dlq_id} not found.")
        if dlq_entry.replayed_at is not None:
            raise DeadLetterEntryNotReplayableException("This entry has already been replayed.")
        if dlq_entry.resume_id is None:
            raise DeadLetterEntryNotReplayableException(
                "This entry is not a resume-processing failure and cannot be replayed here."
            )

        resume = self.resume_repo.get_by_id(dlq_entry.resume_id)
        if resume is None:
            raise NotFoundError(f"Resume {dlq_entry.resume_id} not found.")

        campaign = self._resolve_owning_campaign(resume.id)
        if campaign is None:
            raise DeadLetterEntryNotReplayableException(
                "This resume has no linked campaign, so its prompt template cannot be resolved for a replay."
            )
        new_task_id = uuid4()

        try:
            self.resume_repo.mark_parse_pending(resume)
            self.resume_repo.set_task_id(resume, str(new_task_id))
            self.dead_letter_queue_repo.mark_replayed(
                dlq_entry.id, replayed_by=actor_id, replayed_at=datetime.now(timezone.utc),
            )
            self.audit_service.log(
                actor_id=actor_id,
                actor_role=actor_role,
                action_type=ActionType.INDIVIDUAL_UPLOAD_DLQ_REPLAYED,
                entity_type=EntityType.RESUME,
                entity_id=resume.id,
                campaign_id=campaign.id,
                details={
                    "dead_letter_queue_id": str(dlq_entry.id),
                    "original_task_id": dlq_entry.original_task_id,
                    "new_task_id": str(new_task_id),
                },
            )
            self.resume_repo.commit()
        except Exception:
            self.resume_repo.rollback()
            raise

        process_resume_document.apply_async(
            kwargs={
                "resume_id": str(resume.id),
                "prompt_template_id": str(campaign.prompt_template_id),
            },
            task_id=str(new_task_id),
        )
        return resume, new_task_id

    def _resolve_owning_campaign(self, resume_id: UUID):
        """
        Epic 4 (M05-E04) Phase D10 - a resume can in principle be linked
        to more than one campaign; the most recently linked one wins,
        mirroring the same tie-break convention already established by
        CampaignCandidateRepository.get_most_recent_campaign_for_candidate
        (Epic 3 Phase C4). Returns None if the resume has no campaign
        link at all - callers dispatch with prompt_template_id=None in
        that case rather than failing outright.
        """
        if self.campaign_candidate_repo is None or self.campaign_repo is None:
            return None
        campaign_candidates = self.campaign_candidate_repo.get_by_resume_id(resume_id)
        if not campaign_candidates:
            return None
        most_recent = max(campaign_candidates, key=lambda cc: cc.created_at)
        return self.campaign_repo.get_by_id(most_recent.campaign_id)
