from datetime import datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel


class JDReadinessIssue(BaseModel):
    code: str
    message: str


class CampaignReopenReadinessResponse(BaseModel):
    """S04-T01: readiness validation + current config, for the reopen confirmation dialog."""

    is_ready: bool
    issues: list[JDReadinessIssue]

    campaign_id: UUID
    campaign_name: str
    jd_id: UUID
    jd_title: str
    max_candidates: int | None
    candidate_count: int
    deadline: datetime | None
    weight_deterministic: Decimal
    weight_semantic: Decimal
    weight_ai: Decimal


class CampaignReopenResultResponse(BaseModel):
    """S04-T02/T03: result of a successful reopen."""

    campaign_id: UUID
    campaign_name: str
    status: str
    reopened_at: datetime
    deadline_cleared: bool
    original_closure_reason: str | None
    closed_at: datetime | None
    duration_closed_days: float | None
