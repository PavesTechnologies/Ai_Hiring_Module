from collections import Counter, defaultdict
from uuid import UUID

from app.core.encryption_service import EncryptionService
from app.exceptions.bulk_upload_exceptions import BulkUploadJobNotFoundException
from app.exception_handler.exceptions import NotFoundError
from app.models.async_tasks import BulkUploadFileStatus, StageExecutionStatus
from app.repositories.bulk_upload_job_file_repository import BulkUploadJobFileRepository
from app.repositories.bulk_upload_job_repository import BulkUploadJobRepository
from app.repositories.candidate_repository import CandidateRepository
from app.repositories.celery_task_log_repository import CeleryTaskLogRepository
from app.repositories.dead_letter_queue_repository import DeadLetterQueueRepository
from app.repositories.document_processing_repository import DocumentProcessingRepository
from app.repositories.resume_repository import ResumeRepository
from app.repositories.stage_failure_log_repository import StageFailureLogRepository
from app.schemas.bulk_upload.monitoring import (
    BulkFileDetailResponse,
    BulkFileListItem,
    BulkFileListResponse,
    BulkFileTimelineResponse,
    BulkJobFailureItem,
    BulkJobFailureListResponse,
    BulkJobMetricsResponse,
)
from app.schemas.resume.monitoring import (
    CandidateSummary,
    EmbeddingStatus,
    ParserInfo,
    ProcessingSummary,
    ResumeSummary,
    SkillSummary,
)
from app.services.resume.monitoring_shared import build_failure_info, build_stage_timeline_fields


class BulkUploadMonitoringService:
    """
    Read-only monitoring/tracking service for per-file bulk-upload
    visibility (endpoints #5-#7) — mirrors ResumeMonitoringService's shape
    and role, kept separate from BulkUploadService (the write-path service
    backing upload/cancel), whose behavior stays untouched.
    """

    def __init__(
        self,
        bulk_upload_job_repo: BulkUploadJobRepository,
        bulk_upload_job_file_repo: BulkUploadJobFileRepository,
        resume_repository: ResumeRepository,
        candidate_repository: CandidateRepository,
        encryption_service: EncryptionService,
        task_log_repository: CeleryTaskLogRepository,
        stage_repository: DocumentProcessingRepository,
        stage_failure_log_repository: StageFailureLogRepository,
        dead_letter_queue_repository: DeadLetterQueueRepository,
    ):
        self.bulk_upload_job_repo = bulk_upload_job_repo
        self.bulk_upload_job_file_repo = bulk_upload_job_file_repo
        self.resume_repository = resume_repository
        self.candidate_repository = candidate_repository
        self.encryption_service = encryption_service
        self.task_log_repository = task_log_repository
        self.stage_repository = stage_repository
        self.stage_failure_log_repository = stage_failure_log_repository
        self.dead_letter_queue_repository = dead_letter_queue_repository

    def list_files(
        self,
        bulk_upload_job_id: UUID,
        *,
        status: BulkUploadFileStatus | None = None,
        search: str | None = None,
        page: int = 1,
        size: int = 20,
        sort_by: str = "created_at",
        sort_dir: str = "desc",
    ) -> BulkFileListResponse:
        self._get_job_or_404(bulk_upload_job_id)

        files = self.bulk_upload_job_file_repo.search(
            bulk_upload_job_id=bulk_upload_job_id, status=status, search=search,
            page=page, size=size, sort_by=sort_by, sort_dir=sort_dir,
        )
        total = self.bulk_upload_job_file_repo.count_search(
            bulk_upload_job_id=bulk_upload_job_id, status=status, search=search,
        )

        # One batched celery_task_log query for the whole page, not one per file.
        task_ids = [f.task_id for f in files if f.task_id]
        retry_counts = {
            log.task_id: log.retry_count
            for log in self.task_log_repository.get_by_task_ids(task_ids)
        }

        items = [
            BulkFileListItem(
                id=f.id,
                original_filename=f.original_filename,
                status=f.status.value,
                task_id=f.task_id,
                retry_count=retry_counts.get(f.task_id),
                created_at=f.created_at,
            )
            for f in files
        ]
        return BulkFileListResponse(items=items, total=total, page=page, size=size)

    def get_file_detail(self, bulk_upload_job_id: UUID, file_id: UUID) -> BulkFileDetailResponse:
        self._get_job_or_404(bulk_upload_job_id)
        job_file = self._get_file_or_404(bulk_upload_job_id, file_id)

        resume = self.resume_repository.get_by_file_path(job_file.storage_path)
        candidate = self.candidate_repository.get_by_id(resume.candidate_id) if resume else None

        task_log = self.task_log_repository.get_by_task_id(job_file.task_id) if job_file.task_id else None
        executions = self.stage_repository.get_by_task_id(job_file.task_id) if job_file.task_id else []

        resume_summary = None
        candidate_summary = None
        skill_summary = None
        embedding_status = None
        parser_info = None
        if resume is not None:
            resume_summary = ResumeSummary(
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
            )
            if candidate is not None:
                candidate_summary = CandidateSummary(
                    id=candidate.id,
                    full_name=self.encryption_service.decrypt(
                        candidate.full_name_encrypted, candidate.encryption_key_id
                    ),
                    email=self.encryption_service.decrypt(candidate.email_encrypted, candidate.encryption_key_id),
                    jurisdiction=candidate.jurisdiction,
                    consent_given=candidate.consent_given,
                )

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

            parse_attempts = self.resume_repository.get_parse_attempts(resume.id)
            parser_used = parse_attempts[-1].parser_used if parse_attempts else None
            parser_info = ParserInfo(parser_used=parser_used, parser_version=resume.parser_version)

        failure = None
        if job_file.status == BulkUploadFileStatus.FAILED:
            failure_logs = (
                self.stage_failure_log_repository.get_by_task_id(job_file.task_id) if job_file.task_id else []
            )
            dlq_entry = (
                self.dead_letter_queue_repository.get_by_task_id(job_file.task_id) if job_file.task_id else None
            )
            failure = build_failure_info(executions, failure_logs, dlq_entry, task_log)

        return BulkFileDetailResponse(
            file_id=job_file.id,
            bulk_upload_job_id=job_file.bulk_upload_job_id,
            original_filename=job_file.original_filename,
            file_status=job_file.status.value,
            task_id=job_file.task_id,
            resume=resume_summary,
            candidate=candidate_summary,
            processing=ProcessingSummary(
                task_id=job_file.task_id,
                current_status=task_log.status.value if task_log else None,
                current_stage=executions[-1].stage.value if executions else None,
                attempt_number=(task_log.retry_count + 1) if task_log else None,
                retry_count=task_log.retry_count if task_log else None,
            ),
            skill_summary=skill_summary,
            embedding_status=embedding_status,
            parser_info=parser_info,
            failure=failure,
        )

    def get_file_timeline(
        self, bulk_upload_job_id: UUID, file_id: UUID, attempt_number: int | None = None,
    ) -> BulkFileTimelineResponse:
        self._get_job_or_404(bulk_upload_job_id)
        job_file = self._get_file_or_404(bulk_upload_job_id, file_id)

        if not job_file.task_id:
            raise NotFoundError(f"No processing task has been scheduled for file {file_id} yet.")

        task_log = self.task_log_repository.get_by_task_id(job_file.task_id)
        if task_log is None:
            raise NotFoundError(f"No task log found for file {file_id}.")

        executions = self.stage_repository.get_by_task_id(job_file.task_id)
        failure_logs = self.stage_failure_log_repository.get_by_task_id(job_file.task_id)

        fields = build_stage_timeline_fields(job_file.task_id, task_log, executions, failure_logs, attempt_number)
        return BulkFileTimelineResponse(file_id=job_file.id, **fields)

    def get_job_metrics(self, bulk_upload_job_id: UUID) -> BulkJobMetricsResponse:
        """
        total_files/processed/failed/duplicate come straight off the job
        row's own counters (already maintained atomically per-file by
        bulk_upload_tasks.py) rather than being recomputed here. Only
        avg_duration_by_stage/retry_rate genuinely need a fresh aggregate
        over this job's files' stage executions and task logs.
        """
        job = self._get_job_or_404(bulk_upload_job_id)
        files = self.bulk_upload_job_file_repo.get_by_job_id(bulk_upload_job_id)
        task_ids = [f.task_id for f in files if f.task_id]

        durations_by_stage = defaultdict(list)
        for execution in self.stage_repository.get_by_task_ids(task_ids):
            if execution.duration_ms is not None:
                durations_by_stage[execution.stage.value].append(execution.duration_ms)
        avg_duration_by_stage = {
            stage: round(sum(durations) / len(durations), 1) for stage, durations in durations_by_stage.items()
        }

        task_logs = self.task_log_repository.get_by_task_ids(task_ids)
        retried = sum(1 for log in task_logs if log.retry_count > 0)
        retry_rate = round(retried / len(task_logs), 4) if task_logs else 0.0
        success_rate = round(job.processed_count / job.total_files, 4) if job.total_files else 0.0

        return BulkJobMetricsResponse(
            bulk_upload_job_id=job.id,
            total_files=job.total_files,
            processed=job.processed_count,
            failed=job.failed_count,
            duplicate=job.duplicate_count,
            avg_duration_by_stage=avg_duration_by_stage,
            retry_rate=retry_rate,
            success_rate=success_rate,
        )

    def get_job_failures(
        self, bulk_upload_job_id: UUID, *, page: int = 1, size: int = 20,
    ) -> BulkJobFailureListResponse:
        self._get_job_or_404(bulk_upload_job_id)

        files = self.bulk_upload_job_file_repo.search(
            bulk_upload_job_id=bulk_upload_job_id, status=BulkUploadFileStatus.FAILED, page=page, size=size,
        )
        total = self.bulk_upload_job_file_repo.count_search(
            bulk_upload_job_id=bulk_upload_job_id, status=BulkUploadFileStatus.FAILED,
        )

        task_ids = [f.task_id for f in files if f.task_id]
        task_logs_by_id = {log.task_id: log for log in self.task_log_repository.get_by_task_ids(task_ids)}
        executions_by_task = defaultdict(list)
        for execution in self.stage_repository.get_by_task_ids(task_ids):
            executions_by_task[execution.task_id].append(execution)
        failure_logs_by_task = defaultdict(list)
        for failure in self.stage_failure_log_repository.get_by_task_ids(task_ids):
            failure_logs_by_task[failure.task_id].append(failure)

        items = []
        for job_file in files:
            task_log = task_logs_by_id.get(job_file.task_id)
            executions = executions_by_task.get(job_file.task_id, [])
            failure_logs = failure_logs_by_task.get(job_file.task_id, [])

            failed_execution = next(
                (e for e in reversed(executions) if e.status == StageExecutionStatus.FAILED), None,
            )
            info = build_failure_info(executions, failure_logs, dlq_entry=None, task_log=task_log)

            items.append(
                BulkJobFailureItem(
                    file_id=job_file.id,
                    original_filename=job_file.original_filename,
                    failed_stage=info.failed_stage,
                    error_message=info.error_message,
                    classification=info.classification,
                    retry_count=task_log.retry_count if task_log else None,
                    failed_at=failed_execution.completed_at if failed_execution else None,
                )
            )

        return BulkJobFailureListResponse(items=items, total=total, page=page, size=size)

    def _get_job_or_404(self, bulk_upload_job_id: UUID):
        job = self.bulk_upload_job_repo.get_by_id(bulk_upload_job_id)
        if job is None:
            raise BulkUploadJobNotFoundException("Bulk upload job not found.")
        return job

    def _get_file_or_404(self, bulk_upload_job_id: UUID, file_id: UUID):
        job_file = self.bulk_upload_job_file_repo.get_by_id_and_job(file_id, bulk_upload_job_id)
        if job_file is None:
            raise NotFoundError(f"File {file_id} not found in bulk upload job {bulk_upload_job_id}.")
        return job_file
