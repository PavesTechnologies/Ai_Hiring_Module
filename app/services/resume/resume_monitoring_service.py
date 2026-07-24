from collections import Counter
from datetime import datetime
from uuid import UUID

from app.core.encryption_service import EncryptionService
from app.exception_handler.exceptions import NotFoundError
from app.models.candidates import ParseStatus
from app.repositories.candidate_repository import CandidateRepository
from app.repositories.celery_task_log_repository import CeleryTaskLogRepository
from app.repositories.dead_letter_queue_repository import DeadLetterQueueRepository
from app.repositories.document_processing_repository import DocumentProcessingRepository
from app.repositories.resume_repository import ResumeRepository
from app.repositories.stage_failure_log_repository import StageFailureLogRepository
from app.schemas.resume.monitoring import (
    CandidateSummary,
    EmbeddingStatus,
    ParseAttemptItem,
    ParserInfo,
    ProcessingSummary,
    ResumeDetailResponse,
    ResumeListItem,
    ResumeListResponse,
    ResumeSummary,
    ResumeTimelineResponse,
    SkillSummary,
)
from app.services.resume.monitoring_shared import build_failure_info, build_stage_timeline_fields

CANDIDATE_PII_PURPOSE = "CANDIDATE_PII"


class ResumeMonitoringService:
    """
    Read-only monitoring/tracking service for the frontend UI — does not
    write to any table the processing pipeline owns. Deliberately separate
    from ResumeProcessingStatusService (which backs the existing production
    GET /resumes/processing-status/{task_id} endpoint) rather than an
    extension of it, so that endpoint's response shape stays untouched.
    """

    def __init__(
        self,
        resume_repository: ResumeRepository,
        candidate_repository: CandidateRepository,
        encryption_service: EncryptionService,
        task_log_repository: CeleryTaskLogRepository,
        stage_repository: DocumentProcessingRepository,
        stage_failure_log_repository: StageFailureLogRepository,
        dead_letter_queue_repository: DeadLetterQueueRepository,
    ):
        self.resume_repository = resume_repository
        self.candidate_repository = candidate_repository
        self.encryption_service = encryption_service
        self.task_log_repository = task_log_repository
        self.stage_repository = stage_repository
        self.stage_failure_log_repository = stage_failure_log_repository
        self.dead_letter_queue_repository = dead_letter_queue_repository

    def get_timeline(self, resume_id: UUID, attempt_number: int | None = None) -> ResumeTimelineResponse:
        """
        attempt_number is optional and defaults to the current/latest
        attempt. This matters because a genuine Celery retry re-runs every
        stage from TEXT_EXTRACTION again — nothing is skipped on a real
        retry (initial_context is never passed for individual upload) — so
        document_processing_stage_executions accumulates a full set of 7
        rows per attempt, not just for the stage that failed. Without
        filtering to one attempt, a retried resume would show duplicate
        entries for the same stage (e.g. two AI_EXTRACTION rows, one FAILED
        at attempt 1 and one SUCCESS at attempt 2) instead of one clean
        7-stage timeline.
        """
        resume = self._get_resume_or_404(resume_id)
        task_id = self._require_task_id(resume)

        task_log = self.task_log_repository.get_by_task_id(task_id)
        if task_log is None:
            raise NotFoundError(f"No task log found for resume {resume_id}.")

        executions = self.stage_repository.get_by_task_id(task_id)
        failure_logs = self.stage_failure_log_repository.get_by_task_id(task_id)

        fields = build_stage_timeline_fields(task_id, task_log, executions, failure_logs, attempt_number)
        return ResumeTimelineResponse(resume_id=resume.id, **fields)

    def get_parse_attempts(self, resume_id: UUID) -> list[ParseAttemptItem]:
        resume = self._get_resume_or_404(resume_id)

        items = [
            ParseAttemptItem(
                source="parse_attempt",
                attempt_number=attempt.attempt_number,
                stage=None,
                parser_used=attempt.parser_used,
                parser_version=attempt.parser_version,
                status=attempt.status.value,
                error_code=attempt.error_code,
                error_detail=attempt.error_detail,
                confidence_score=attempt.confidence_score,
                duration_ms=attempt.duration_ms,
                occurred_at=attempt.attempted_at,
            )
            for attempt in self.resume_repository.get_parse_attempts(resume.id)
        ]

        if resume.task_id:
            # resume_parse_attempts only ever records a *successful* attempt
            # (see docs/Resume_Intake_Monitoring_API_Design.md, Gap 2) — a
            # resume that failed before ever reaching PERSISTENCE has zero
            # rows there. stage_failure_logs is where that history actually
            # lives, so it's merged in here rather than silently omitted.
            items.extend(
                ParseAttemptItem(
                    source="stage_failure",
                    attempt_number=failure.attempt_number,
                    stage=failure.stage.value,
                    parser_used=None,
                    parser_version=None,
                    status=failure.classification.value,
                    error_code=failure.exception_type,
                    error_detail=failure.message,
                    confidence_score=None,
                    duration_ms=None,
                    occurred_at=failure.created_at,
                )
                for failure in self.stage_failure_log_repository.get_by_task_id(resume.task_id)
            )

        items.sort(key=lambda item: item.occurred_at)
        return items

    def get_resume_detail(self, resume_id: UUID) -> ResumeDetailResponse:
        resume = self._get_resume_or_404(resume_id)

        candidate = self.candidate_repository.get_by_id(resume.candidate_id)
        if candidate is None:
            raise NotFoundError(f"Candidate for resume {resume_id} not found.")

        full_name = self.encryption_service.decrypt(candidate.full_name_encrypted, candidate.encryption_key_id)
        email = self.encryption_service.decrypt(candidate.email_encrypted, candidate.encryption_key_id)

        task_log = self.task_log_repository.get_by_task_id(resume.task_id) if resume.task_id else None
        executions = self.stage_repository.get_by_task_id(resume.task_id) if resume.task_id else []

        skills = self.resume_repository.get_candidate_skills(resume.id)
        skill_summary = SkillSummary(
            total_skills=len(skills),
            matched=sum(1 for skill in skills if skill.canonical_skill_id is not None),
            unmatched=sum(1 for skill in skills if skill.canonical_skill_id is None),
            by_tier=dict(Counter(skill.match_tier for skill in skills)),
        )

        embedding = self.resume_repository.get_embedding(resume.id)
        embedding_status = EmbeddingStatus(
            exists=embedding is not None,
            embedding_model_version_id=embedding.embedding_model_version_id if embedding else None,
            generated_at=embedding.created_at if embedding else None,
        )

        # parser_used isn't a column on Resume itself — only recorded per
        # attempt in resume_parse_attempts — so pull it from the most
        # recent attempt rather than leaving it always null.
        parse_attempts = self.resume_repository.get_parse_attempts(resume.id)
        parser_used = parse_attempts[-1].parser_used if parse_attempts else None

        failure = None
        if resume.parse_status == ParseStatus.FAILED:
            failure_logs = self.stage_failure_log_repository.get_by_task_id(resume.task_id) if resume.task_id else []
            dlq_entry = self.dead_letter_queue_repository.get_by_task_id(resume.task_id) if resume.task_id else None
            failure = build_failure_info(executions, failure_logs, dlq_entry, task_log)

        return ResumeDetailResponse(
            resume=ResumeSummary(
                id=resume.id,
                file_path=resume.file_path,
                file_format=resume.file_format.value,
                version_number=resume.version_number,
                is_active_version=resume.is_active_version,
                parse_status=resume.parse_status.value,
                parser_version=resume.parser_version,
                page_count=resume.page_count,
                created_at=resume.created_at,
                bulk_upload_job_id=resume.bulk_upload_job_id,
            ),
            candidate=CandidateSummary(
                id=candidate.id,
                full_name=full_name,
                email=email,
                jurisdiction=candidate.jurisdiction,
                consent_given=candidate.consent_given,
            ),
            processing=ProcessingSummary(
                task_id=resume.task_id,
                current_status=task_log.status.value if task_log else None,
                current_stage=executions[-1].stage.value if executions else None,
                attempt_number=(task_log.retry_count + 1) if task_log else None,
                retry_count=task_log.retry_count if task_log else None,
            ),
            skill_summary=skill_summary,
            embedding_status=embedding_status,
            parser_info=ParserInfo(parser_used=parser_used, parser_version=resume.parser_version),
            failure=failure,
        )

    def list_resumes(
        self,
        *,
        campaign_id: UUID | None = None,
        parse_status: ParseStatus | None = None,
        source: str | None = None,
        email_hash: str | None = None,
        uploaded_from: datetime | None = None,
        uploaded_to: datetime | None = None,
        page: int = 1,
        size: int = 20,
        sort_by: str = "created_at",
        sort_dir: str = "desc",
    ) -> ResumeListResponse:
        filters = dict(
            campaign_id=campaign_id,
            parse_status=parse_status,
            source=source,
            email_hash=email_hash,
            uploaded_from=uploaded_from,
            uploaded_to=uploaded_to,
        )
        resumes = self.resume_repository.search(
            **filters, page=page, size=size, sort_by=sort_by, sort_dir=sort_dir,
        )
        total = self.resume_repository.count_search(**filters)

        # One batched candidate lookup for the whole page, not one query per row.
        candidates_by_id = {
            candidate.id: candidate
            for candidate in self.candidate_repository.get_by_ids([r.candidate_id for r in resumes])
        }

        items = []
        for resume in resumes:
            candidate = candidates_by_id.get(resume.candidate_id)
            full_name = (
                self.encryption_service.decrypt(candidate.full_name_encrypted, candidate.encryption_key_id)
                if candidate else "Unknown"
            )
            email = (
                self.encryption_service.decrypt(candidate.email_encrypted, candidate.encryption_key_id)
                if candidate else "Unknown"
            )
            items.append(
                ResumeListItem(
                    id=resume.id,
                    candidate_id=resume.candidate_id,
                    candidate_full_name=full_name,
                    candidate_email=email,
                    file_format=resume.file_format.value,
                    parse_status=resume.parse_status.value,
                    version_number=resume.version_number,
                    is_active_version=resume.is_active_version,
                    source="bulk" if resume.bulk_upload_job_id else "individual",
                    bulk_upload_job_id=resume.bulk_upload_job_id,
                    created_at=resume.created_at,
                )
            )

        return ResumeListResponse(items=items, total=total, page=page, size=size)

    def _get_resume_or_404(self, resume_id: UUID):
        resume = self.resume_repository.get_by_id(resume_id)
        if resume is None:
            raise NotFoundError(f"Resume {resume_id} not found.")
        return resume

    @staticmethod
    def _require_task_id(resume) -> str:
        if not resume.task_id:
            raise NotFoundError(
                f"No processing task has been scheduled for resume {resume.id} yet."
            )
        return resume.task_id
