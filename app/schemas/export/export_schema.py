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


class ExportPreviewResponse(BaseModel):
    """Lets the export dialog say what will happen before the user commits."""

    row_count: int
    threshold: int
    will_be_async: bool
    notify_email_hint: str | None = None


class BatchScorecardRequest(BaseModel):
    campaign_candidate_ids: list[UUID] = Field(..., min_length=2, max_length=100)
    # SINGLE_PDF concatenates with page breaks; ZIP is one PDF per candidate.
    format: str = Field(default="PDF", pattern="^(PDF|ZIP)$")


class ScheduledExportConfigRequest(BaseModel):
    enabled: bool = True
    frequency: str = Field(..., description="DAILY | WEEKLY | BIWEEKLY")
    day_of_week: str | None = Field(default=None, description="Required for WEEKLY/BIWEEKLY.")
    time: str = Field(default="09:00", description="HH:MM, 24-hour.")
    top_n: int = Field(default=10, ge=1, le=50)
    format: str = Field(default="XLSX", description="XLSX | PDF")
    recipients: list[EmailStr] = Field(..., min_length=1)


class ScheduledExportPauseRequest(BaseModel):
    paused: bool


class ScheduledExportConfigResponse(BaseModel):
    campaign_id: UUID
    configured: bool
    enabled: bool | None = None
    paused: bool | None = None
    # True when the campaign itself is not ACTIVE — the schedule is suspended
    # regardless of its own enabled/paused flags.
    auto_suspended: bool | None = None
    frequency: str | None = None
    day_of_week: str | None = None
    time: str | None = None
    top_n: int | None = None
    format: str | None = None
    recipients: list[str] = []
    last_sent_at: str | None = None
    next_run_at: str | None = None


class ScheduledExportHistoryEntry(BaseModel):
    task_id: str
    title: str | None = None
    generated_at: datetime | None = None
    status: str
    scheduled: bool
    duration_ms: int | None = None
    download_url: str | None = None
    error: str | None = None
    failed: bool


class DsarRequest(BaseModel):
    """
    The email is used only to compute the lookup hash and is never persisted
    (M11-E05-S04-T03).
    """

    email: EmailStr
