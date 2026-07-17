from uuid import UUID

from pydantic import BaseModel


class ResumeUploadAcceptedResponse(BaseModel):
    resume_id: UUID
    campaign_candidate_id: UUID
    task_id: UUID
    candidate_name_masked: str
    file_name: str
    campaign_name: str
    pipeline_stage: str
    parse_status: str


class StageProgress(BaseModel):
    stage: str
    status: str
    error_message: str | None
    duration_ms: int | None


class ResumeProcessingStatusResponse(BaseModel):
    task_id: UUID
    overall_status: str
    current_stage: str | None
    stages: list[StageProgress]
    resume_id: UUID | None
    error_message: str | None
