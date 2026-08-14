from datetime import date as date_type, datetime, time as time_type
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models.interview import InterviewPlatform


class InterviewerInput(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    email: str = Field(..., min_length=1, max_length=255)


class InterviewerResponse(BaseModel):
    id: UUID
    name: str
    email: str

    model_config = ConfigDict(from_attributes=True)


class ScheduleInterviewRequest(BaseModel):
    """
    Epic 4 Step 3: wire format for POST .../interviews. Required fields
    mirror InterviewSchedule's own non-nullable columns plus the time
    fields it's built from (date/start_time/end_time -> start_at/end_at);
    interview_type/platform/location/notes are optional here because the
    model itself declares them nullable. duration_minutes is accepted for
    parity with the response shape but never trusted for storage - the
    canonical duration is always derived from start_at/end_at, since the
    client already sends both explicit times and a redundant duration
    could disagree with them.
    """
    interview_type: Optional[str] = Field(default=None, max_length=100)
    interviewers: list[InterviewerInput] = Field(..., min_length=1)
    date: date_type
    start_time: time_type
    end_time: time_type
    duration_minutes: Optional[int] = None
    platform: Optional[InterviewPlatform] = None
    location: Optional[str] = None
    notes: Optional[str] = None

    @model_validator(mode="after")
    def _end_after_start(self):
        if self.end_time <= self.start_time:
            raise ValueError("end_time must be after start_time.")
        return self


class RescheduleInterviewRequest(ScheduleInterviewRequest):
    reason: Optional[str] = None


class CancelInterviewRequest(BaseModel):
    reason: str = Field(..., min_length=1)


class InterviewHistoryEntryResponse(BaseModel):
    id: UUID
    old_scheduled_at: Optional[datetime]
    new_scheduled_at: Optional[datetime]
    rescheduled_by: str
    reason: Optional[str]
    changed_at: datetime


class InterviewScheduleResponse(BaseModel):
    id: UUID
    campaign_candidate_id: UUID
    status: str
    interview_type: Optional[str]
    interviewers: list[InterviewerResponse]
    date: date_type
    start_time: time_type
    end_time: time_type
    duration_minutes: int
    platform: Optional[InterviewPlatform]
    location: Optional[str]
    notes: Optional[str]
    cancel_reason: Optional[str]
    meeting_link: Optional[str]
    created_at: datetime
    history: list[InterviewHistoryEntryResponse]
