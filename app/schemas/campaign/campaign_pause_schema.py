from pydantic import BaseModel


class PauseImpactSummaryResponse(BaseModel):

    candidate_count: int
    queued_task_count: int          # celery_task_log QUEUED or RUNNING for this campaign
    processing_bulk_job_count: int  # bulk_upload_jobs in PROCESSING
    warning: str = (
        "Pausing will stop new uploads, halt queued processing tasks, and "
        "suspend automated pipeline progression."
    )


class ResumeSummaryResponse(BaseModel):
    """S02-T01: data for the resume confirmation dialog."""

    paused_task_count: int          # celery_task_log with status = PAUSED
    pending_resume_count: int       # resumes with parse_status = PENDING for this campaign
    estimated_processing_seconds: int | None = None
    warning: str = (
        "Confirming will re-queue all suspended tasks and re-enable uploads."
    )
