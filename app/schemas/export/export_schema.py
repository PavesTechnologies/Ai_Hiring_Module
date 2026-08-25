from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, EmailStr, Field


class ExportDispatchResponse(BaseModel):
    """
    M11-E05-S01-T03 — returned when an export is too large to generate inline.

    `synchronous=False` is the signal the UI needs: it means no file is coming
    back on this request and the user should watch the progress panel instead.
    """

    synchronous: bool = False
    task_id: str | None = None
    row_count: int
    threshold: int
    detail: str


class BatchScorecardRequest(BaseModel):
    campaign_candidate_ids: list[UUID] = Field(..., min_length=2, max_length=100)
    # SINGLE_PDF concatenates with page breaks; ZIP is one PDF per candidate.
    format: str = Field(default="PDF", pattern="^(PDF|ZIP)$")
