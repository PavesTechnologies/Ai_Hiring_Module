from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Security, status

from app.dependencies.monitoring import get_ops_monitoring_service
from app.enums.constants import UserRole
from app.middleware.rbac import TokenUser, require_roles
from app.schemas.monitoring import (
    ProcessingMetricsResponse,
    QueueStatusResponse,
    UploadQueueDashboardResponse,
)
from app.schemas.response import APIResponse
from app.services.ops_monitoring_service import OpsMonitoringService

router = APIRouter(
    prefix="/monitoring",
    tags=["Ops Monitoring"],
)


@router.get(
    "/queue-status",
    response_model=APIResponse[QueueStatusResponse],
    status_code=status.HTTP_200_OK,
)
def get_queue_status(
    campaign_id: UUID | None = Query(default=None),
    service: OpsMonitoringService = Depends(get_ops_monitoring_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN) | UserRole.RECRUITER | UserRole.SUPER_ADMIN | UserRole.HIRING_MANAGER),
):
    """
    Read-only ops monitoring endpoint — a database approximation of queue
    depth (COUNT(*) queries against celery_task_log/bulk_upload_job_files),
    not a live broker read.
    """
    return APIResponse.ok(
        data=service.get_queue_status(campaign_id=campaign_id),
        message="Queue status retrieved successfully.",
    )


@router.get(
    "/processing-metrics",
    response_model=APIResponse[ProcessingMetricsResponse],
    status_code=status.HTTP_200_OK,
)
def get_processing_metrics(
    window: Literal["1h", "24h", "7d"] = Query(default="24h"),
    service: OpsMonitoringService = Depends(get_ops_monitoring_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN) | UserRole.RECRUITER | UserRole.SUPER_ADMIN | UserRole.HIRING_MANAGER),
):
    """
    Read-only ops monitoring endpoint — aggregates bounded to a fixed time
    window rather than an always-fresh unbounded aggregate, so this stays
    cheap enough to run live with no new background pre-aggregation job.
    """
    return APIResponse.ok(
        data=service.get_processing_metrics(window=window),
        message="Processing metrics retrieved successfully.",
    )


@router.get(
    "/upload-queue-dashboard",
    response_model=APIResponse[UploadQueueDashboardResponse],
    status_code=status.HTTP_200_OK,
)
def get_upload_queue_dashboard(
    service: OpsMonitoringService = Depends(get_ops_monitoring_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN) | UserRole.RECRUITER | UserRole.SUPER_ADMIN | UserRole.HIRING_MANAGER),
):
    """
    Epic 4 (M05-E04) Phase D12 — read-only, platform-wide upload queue
    dashboard: pending/queued/running/dead resume-intake counts, circuit
    breaker health for the two upload-critical external services, and a
    per-campaign queue-depth breakdown (capped, deterministically sorted).
    """
    return APIResponse.ok(
        data=service.get_upload_queue_dashboard(),
        message="Upload queue dashboard retrieved successfully.",
    )
