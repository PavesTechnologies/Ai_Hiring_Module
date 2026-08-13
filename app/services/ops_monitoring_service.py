import json
from datetime import datetime, timedelta, timezone
from uuid import UUID

from app.models.async_tasks import BulkUploadFileStatus, BulkUploadStatus, DocumentType, TaskStatus
from app.models.candidates import ParseStatus
from app.models.config import CBState
from app.repositories.bulk_upload_job_file_repository import BulkUploadJobFileRepository
from app.repositories.bulk_upload_job_repository import BulkUploadJobRepository
from app.repositories.CampaignRepository import CampaignRepository
from app.repositories.celery_task_log_repository import CeleryTaskLogRepository
from app.repositories.circuit_breaker_repository import CircuitBreakerRepository
from app.repositories.document_processing_repository import DocumentProcessingRepository
from app.repositories.resume_repository import ResumeRepository
from app.repositories.stage_failure_log_repository import StageFailureLogRepository
from app.schemas.monitoring import (
    CampaignQueueBreakdownItem,
    CircuitBreakerStatusItem,
    FailureReasonItem,
    ProcessingMetricsResponse,
    QueueStatusResponse,
    UploadQueueDashboardResponse,
)
from app.core.config import settings
from app.core.cache_keys import dashboard_key
from app.services.cache_service import CacheService

# Mirrors the literal task_type strings each task logs itself with —
# app/tasks/resume_processing_tasks.py's RESUME_DOCUMENT_PROCESSING_TASK_TYPE
# and app/tasks/bulk_upload_tasks.py's BULK_RESUME_PARSE_TASK_TYPE. Not
# imported directly to avoid pulling the Celery task modules (and their
# storage/AI client dependencies) into a read-only monitoring service.
RESUME_TASK_TYPE = "RESUME_DOCUMENT_PROCESSING"
BULK_FILE_TASK_TYPE = "BULK_RESUME_PARSE"
RESUME_INTAKE_TASK_TYPES = [RESUME_TASK_TYPE, BULK_FILE_TASK_TYPE]

# Epic 4 (M05-E04) Phase D12 — the only two external dependencies whose
# health actually affects resume-intake uploads; not a generic "list every
# circuit breaker row" view.
_UPLOAD_QUEUE_CIRCUIT_BREAKERS = ["SUPABASE_STORAGE", "ENCRYPTION_SERVICE"]

# Named per the approved D12 refinement (replacing a bare literal 20) —
# bounds the per-campaign breakdown so the dashboard stays cheap even on a
# platform with hundreds of campaigns.
_CAMPAIGN_BREAKDOWN_LIMIT = 20

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
        resume_repository: ResumeRepository | None = None,
        bulk_upload_job_repository: BulkUploadJobRepository | None = None,
        campaign_repository: CampaignRepository | None = None,
        circuit_breaker_repository: CircuitBreakerRepository | None = None,
        cache_service: CacheService | None = None,
    ):
        self.celery_task_log_repository = celery_task_log_repository
        self.bulk_upload_job_file_repository = bulk_upload_job_file_repository
        self.stage_repository = stage_repository
        self.stage_failure_log_repository = stage_failure_log_repository
        # Epic 4 (M05-E04) Phase D12 — optional, only required by
        # get_upload_queue_dashboard(); kept optional so this constructor
        # stays backward compatible with any other caller.
        self.resume_repository = resume_repository
        self.bulk_upload_job_repository = bulk_upload_job_repository
        self.campaign_repository = campaign_repository
        self.circuit_breaker_repository = circuit_breaker_repository
        self.cache_service = cache_service

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
        if not self.cache_service:
            return self._load_processing_metrics(window)
        raw = self.cache_service.get_or_set(
            dashboard_key("processing-metrics", {"window": window}),
            loader=lambda: self._load_processing_metrics(window).model_dump_json(),
            ttl=settings.cache_dashboard_ttl_seconds,
        )
        return ProcessingMetricsResponse.model_validate_json(raw)

    def _load_processing_metrics(self, window: str) -> ProcessingMetricsResponse:
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

    def get_upload_queue_dashboard(self) -> UploadQueueDashboardResponse:
        if not self.cache_service:
            return self._load_upload_queue_dashboard()
        raw = self.cache_service.get_or_set(
            dashboard_key("upload-queue", {}),
            loader=lambda: self._load_upload_queue_dashboard().model_dump_json(),
            ttl=settings.cache_dashboard_ttl_seconds,
        )
        return UploadQueueDashboardResponse.model_validate_json(raw)

    def _load_upload_queue_dashboard(self) -> UploadQueueDashboardResponse:
        """
        Epic 4 (M05-E04) Phase D12 — platform-wide upload queue dashboard.
        Composes existing platform-wide metrics (queue status, PENDING
        resumes, DEAD tasks) with a per-campaign breakdown and circuit
        breaker health, all read-only.
        """
        queue_status = self.get_queue_status(campaign_id=None)

        pending_resumes_count = self.resume_repository.count_search(parse_status=ParseStatus.PENDING)

        processing_bulk_jobs_count = self.bulk_upload_job_repository.count_by_status(
            BulkUploadStatus.PROCESSING,
        )

        dead_tasks_count = sum(
            self.celery_task_log_repository.count_by_task_type_and_statuses(
                task_type, [TaskStatus.DEAD], campaign_id=None,
            )
            for task_type in RESUME_INTAKE_TASK_TYPES
        )

        circuit_breaker_states = [
            self.circuit_breaker_repository.get_by_service_name(service_name)
            for service_name in _UPLOAD_QUEUE_CIRCUIT_BREAKERS
        ]
        circuit_breakers = [
            CircuitBreakerStatusItem(
                service_name=state.service_name,
                state=state.state.value,
                failure_count=state.failure_count,
                opened_at=state.opened_at,
            )
            for state in circuit_breaker_states
            if state is not None
        ]
        any_circuit_breaker_open = any(
            state.state == CBState.OPEN for state in circuit_breaker_states if state is not None
        )

        campaign_breakdown = self._build_campaign_breakdown()

        return UploadQueueDashboardResponse(
            generated_at=datetime.now(timezone.utc),
            pending_resumes_count=pending_resumes_count,
            resumes_queued=queue_status.resumes_queued,
            resumes_running=queue_status.resumes_running,
            processing_bulk_jobs_count=processing_bulk_jobs_count,
            dead_tasks_count=dead_tasks_count,
            circuit_breakers=circuit_breakers,
            any_circuit_breaker_open=any_circuit_breaker_open,
            campaign_breakdown=campaign_breakdown,
        )

    def _build_campaign_breakdown(self) -> list[CampaignQueueBreakdownItem]:
        pending_by_campaign = {
            campaign_id: (name, count)
            for campaign_id, name, count in self.campaign_repository.get_pending_resume_counts_by_campaign()
        }
        queued_by_campaign = {
            campaign_id: (name, count)
            for campaign_id, name, count in self.campaign_repository.get_queued_resume_task_counts_by_campaign(
                RESUME_TASK_TYPE,
            )
        }

        campaign_ids = set(pending_by_campaign) | set(queued_by_campaign)
        items = []
        for campaign_id in campaign_ids:
            name = (pending_by_campaign.get(campaign_id) or queued_by_campaign.get(campaign_id))[0]
            pending_count = pending_by_campaign.get(campaign_id, (name, 0))[1]
            queued_count = queued_by_campaign.get(campaign_id, (name, 0))[1]
            items.append(
                CampaignQueueBreakdownItem(
                    campaign_id=campaign_id,
                    campaign_name=name,
                    pending_resumes_count=pending_count,
                    queued_resumes_count=queued_count,
                    queue_depth=pending_count + queued_count,
                )
            )

        # Deterministic secondary sort key (campaign_name) so campaigns
        # tied on queue_depth have a stable, reproducible order rather
        # than depending on incidental dict/DB row ordering.
        items.sort(key=lambda item: (-item.queue_depth, item.campaign_name))
        return items[:_CAMPAIGN_BREAKDOWN_LIMIT]
