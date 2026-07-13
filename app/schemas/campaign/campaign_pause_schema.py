from pydantic import BaseModel


class PauseImpactSummaryResponse(BaseModel):

    candidate_count: int
    queued_task_count: int          # celery_task_log QUEUED or RUNNING for this campaign
    processing_bulk_job_count: int  # bulk_upload_jobs in PROCESSING
    warning: str = (
        "Pausing will stop new uploads, halt queued processing tasks, and "
        "suspend automated pipeline progression."
    )
