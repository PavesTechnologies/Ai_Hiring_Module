from datetime import datetime
from enum import Enum

from pydantic import BaseModel


class CampaignClosureReason(str, Enum):
    POSITION_FILLED = "POSITION_FILLED"
    BUDGET_FREEZE = "BUDGET_FREEZE"
    ROLE_CANCELLED = "ROLE_CANCELLED"
    INTAKE_COMPLETE = "INTAKE_COMPLETE"
    OTHER = "OTHER"


class CampaignCloseRequest(BaseModel):
    closure_reason: CampaignClosureReason


class CampaignClosureImpactSummaryResponse(BaseModel):
    """ data for the close confirmation dialog."""

    candidate_count: int
    stage_counts: dict[str, int]
    in_progress_task_count: int          # celery_task_log QUEUED or RUNNING
    pending_human_decision_count: int    # candidates in INTERVIEW or HM_REVIEW
    in_progress_bulk_job_count: int      # bulk_upload_jobs in PROCESSING
    warning: str = (
        "Closing will stop new uploads, cancel queued processing tasks, and "
        "permanently conclude this campaign. This cannot be undone."
    )


class CampaignClosureResultResponse(BaseModel):
    """campaign closure summary, shown to HR_ADMIN and (future) emailed."""

    campaign_id: str
    campaign_name: str
    closed_at: datetime
    closure_reason: CampaignClosureReason
    candidate_count: int
    stage_counts: dict[str, int]
    selected_count: int
    rejected_count: int
    tasks_cancelled_count: int
    bulk_uploads_cancelled_count: int
