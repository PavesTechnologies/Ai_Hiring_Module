"""Scoring v2 Step 3: re-parse & re-score request / parse-changes responses."""
from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


class ReparseRequest(BaseModel):
    # Required (and checked) when a recruiter re-parses a REJECTED candidate -
    # the candidate moves back to SCREENING with it.
    reason: str | None = Field(default=None, max_length=500)


class ReparseResponse(BaseModel):
    campaign_candidate_id: UUID
    resume_id: UUID
    task_id: str
    version_no: int
    moves_to_screening: bool
    stays_rejected: bool
    other_campaigns_sharing_resume: int


class RescoreResponse(BaseModel):
    campaign_candidate_id: UUID
    stage: str
    # What was done: "re-scored (screening)" or "scores updated, stage unchanged (...)".
    rescore: str


class ParseVersionItem(BaseModel):
    version_no: int
    created_at: datetime
    triggered_by: str | None = None
    reason: str | None = None
    # IN_PROGRESS / COMPLETED / FAILED
    status: str
    diff: dict | None = None


class ParseChangesResponse(BaseModel):
    campaign_candidate_id: UUID
    resume_id: UUID
    parse_status: str | None = None
    versions: list[ParseVersionItem] = Field(default_factory=list)
