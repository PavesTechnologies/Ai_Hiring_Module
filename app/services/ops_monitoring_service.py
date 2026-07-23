from datetime import datetime, timedelta, timezone
from uuid import UUID

from app.models.async_tasks import BulkUploadFileStatus, DocumentType, TaskStatus
from app.repositories.bulk_upload_job_file_repository import BulkUploadJobFileRepository
from app.repositories.celery_task_log_repository import CeleryTaskLogRepository
from app.repositories.document_processing_repository import DocumentProcessingRepository
from app.repositories.stage_failure_log_repository import StageFailureLogRepository
from app.schemas.monitoring import FailureReasonItem, ProcessingMetricsResponse, QueueStatusResponse

# Mirrors the literal task_type strings each task logs itself with —
# app/tasks/resume_processing_tasks.py's RESUME_DOCUMENT_PROCESSING_TASK_TYPE
# and app/tasks/bulk_upload_tasks.py's BULK_RESUME_PARSE_TASK_TYPE. Not
# imported directly to avoid pulling the Celery task modules (and their
# storage/AI client dependencies) into a read-only monitoring service.
RESUME_TASK_TYPE = "RESUME_DOCUMENT_PROCESSING"
BULK_FILE_TASK_TYPE = "BULK_RESUME_PARSE"
RESUME_INTAKE_TASK_TYPES = [RESUME_TASK_TYPE, BULK_FILE_TASK_TYPE]

_WINDOW_HOURS = {"1h": 1, "24h": 24, "7d": 24 * 7}


class OpsMonitoringService:
    """
    Read-only ops-wide monitoring (endpoints #10-#11) — a database
    approximation of live queue/throughput state, not a live broker read
    (see docs/Resume_Intake_Monitoring_API_Design.md §7).
    """

    def __init__(
        self,
        celery_task_log_repository: CeleryTaskLogRepository,
        bulk_upload_job_file_repository: BulkUploadJobFileRepository,
        stage_repository: DocumentProcessingRepository,
        stage_failure_log_repository: StageFailureLogRepository,
    ):
        self.celery_task_log_repository = celery_task_log_repository
        self.bulk_upload_job_file_repository = bulk_upload_job_file_repository
        self.stage_repository = stage_repository
        self.stage_failure_log_repository = stage_failure_log_repository

    def get_queue_status(self, campaign_id: UUID | None = None) -> QueueStatusResponse:
        # RETRY is counted alongside RUNNING: a task in RETRY status is
        # actively scheduled to re-run, not idle in the queue the way a
        # fresh QUEUED task is.
        resumes_queued = self.celery_task_log_repository.count_by_task_type_and_statuses(
            RESUME_TASK_TYPE, [TaskStatus.QUEUED], campaign_id=campaign_id,
        )
        resumes_running = self.celery_task_log_repository.count_by_task_type_and_statuses(
            RESUME_TASK_TYPE, [TaskStatus.RUNNING, TaskStatus.RETRY], campaign_id=campaign_id,
        )
        bulk_files_queued = self.bulk_upload_job_file_repository.count_by_status(
            BulkUploadFileStatus.QUEUED, campaign_id=campaign_id,
        )
        bulk_files_running = self.bulk_upload_job_file_repository.count_by_status(
            BulkUploadFileStatus.RUNNING, campaign_id=campaign_id,
        )

        return QueueStatusResponse(
            resumes_queued=resumes_queued,
            resumes_running=resumes_running,
            bulk_files_queued=bulk_files_queued,
            bulk_files_running=bulk_files_running,
        )

    def get_processing_metrics(self, window: str = "24h") -> ProcessingMetricsResponse:
        window_hours = _WINDOW_HOURS[window]
        since = datetime.now(timezone.utc) - timedelta(hours=window_hours)

        completed = self.celery_task_log_repository.count_completed_since(since, RESUME_INTAKE_TASK_TYPES)
        throughput_per_hour = round(completed / window_hours, 2)

        avg_duration_by_stage = self.stage_repository.get_avg_duration_by_stage_since(since, DocumentType.RESUME)
        failure_rate_by_stage = self.stage_repository.get_failure_rate_by_stage_since(since, DocumentType.RESUME)

        top_failure_reasons = [
            FailureReasonItem(exception_type=reason, count=count)
            for reason, count in self.stage_failure_log_repository.get_top_failure_reasons_since(
                since, RESUME_INTAKE_TASK_TYPES,
            )
        ]

        return ProcessingMetricsResponse(
            window=window,
            throughput_per_hour=throughput_per_hour,
            avg_duration_by_stage=avg_duration_by_stage,
            failure_rate_by_stage=failure_rate_by_stage,
            top_failure_reasons=top_failure_reasons,
        )
