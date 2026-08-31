from datetime import date as date_type, datetime, time as time_type
from typing import Optional
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

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

    Timezone-discrepancy fix: `timezone` is required, not defaulted -
    date/start_time/end_time are plain wall-clock values with no way to
    disambiguate what zone they're in on their own, and a silent default
    (e.g. always "UTC") would just reproduce the exact bug this field
    exists to fix for any caller that doesn't explicitly send one. Must be
    a real IANA zone name (e.g. "Asia/Kolkata", "America/New_York") - not
    a raw UTC offset, since offsets don't carry DST rules.
    """
    interview_type: Optional[str] = Field(default=None, max_length=100)
    interviewers: list[InterviewerInput] = Field(..., min_length=1)
    date: date_type
    start_time: time_type
    end_time: time_type
    timezone: str = Field(..., description='IANA timezone name, e.g. "Asia/Kolkata".')
    duration_minutes: Optional[int] = None
    platform: Optional[InterviewPlatform] = None
    location: Optional[str] = None
    notes: Optional[str] = None

    @field_validator("timezone")
    @classmethod
    def _valid_iana_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"'{value}' is not a recognized IANA timezone name.") from exc
        return value

    @model_validator(mode="after")
    def _end_after_start(self):
        if self.end_time <= self.start_time:
            raise ValueError("end_time must be after start_time.")
        return self


class RescheduleInterviewRequest(ScheduleInterviewRequest):
    reason: Optional[str] = None


class CancelInterviewRequest(BaseModel):
    reason: str = Field(..., min_length=1)


class RequestFeedbackResponse(BaseModel):
    """
    Epic 5 - manual "Request Feedback" trigger. 0 is a valid, non-error
    result (nothing left to request), not a special case the frontend
    needs to distinguish from a real send.
    """
    queued_count: int


class CompleteInterviewResponse(BaseModel):
    """
    Epic 5 follow-up - "Mark as Completed". feedback_queued_count covers
    both effects of one action in a single response, so the UI can show
    one confirmation ("Marked as completed - feedback requested from N
    interviewers") instead of two separate calls/messages.
    """
    status: str
    feedback_queued_count: int


class InterviewHistoryEntryResponse(BaseModel):
    id: UUID
    old_scheduled_at: Optional[datetime]
    new_scheduled_at: Optional[datetime]
    rescheduled_by: str
    reason: Optional[str]
    changed_at: datetime


class InterviewScheduleResponse(BaseModel):
    """
    Shared response shape for schedule/reschedule/cancel and the read-only
    GET .../interviews endpoint. date/start_time/end_time/duration_minutes
    are Optional specifically for the GET endpoint's PENDING case (reached
    INTERVIEW, nothing scheduled yet) - InterviewSchedule.start_at/end_at
    are genuinely null on the model until the first successful schedule()
    call, and GET must be able to return that state cleanly rather than
    erroring. schedule/reschedule/cancel never produce a PENDING response
    themselves (each only ever runs after start_at/end_at are set), so
    these 4 fields are effectively always populated outside of GET.
    """
    id: UUID
    campaign_candidate_id: UUID
    round_number: int
    status: str
    interview_type: Optional[str]
    interviewers: list[InterviewerResponse]
    date: Optional[date_type]
    start_time: Optional[time_type]
    end_time: Optional[time_type]
    # The IANA zone date/start_time/end_time are already expressed in -
    # these 3 fields are converted back from the stored UTC instant into
    # this zone, not raw UTC, so what you scheduled is what you get back.
    # Null only for the PENDING/never-scheduled case, same as the 3 above.
    timezone: Optional[str]
    duration_minutes: Optional[int]
    platform: Optional[InterviewPlatform]
    location: Optional[str]
    notes: Optional[str]
    cancel_reason: Optional[str]
    meeting_link: Optional[str]
    created_at: datetime
    history: list[InterviewHistoryEntryResponse]


class CampaignInterviewEntry(BaseModel):
    """
    GET /campaigns/{campaign_id}/interviews - the campaign-wide interview
    calendar. One entry per round across every candidate in the campaign,
    not the full InterviewScheduleResponse shape (no history/notes/
    cancel_reason - this is a calendar view, not a candidate detail page).
    interviewers is active-only, matching the recent interviewer active-
    flag fix - a since-removed interviewer never appears here.
    """
    id: UUID
    campaign_candidate_id: UUID
    candidate_name: str
    round_number: int
    interview_type: Optional[str]
    status: str
    start_at: Optional[datetime]
    end_at: Optional[datetime]
    # start_at/end_at are a genuine UTC instant (tz-aware ISO string with
    # offset) - a frontend can localize them to any viewer directly. This
    # is the zone the round was actually scheduled in, for display
    # purposes (e.g. "2:00 PM IST" alongside a UTC-converted local time).
    timezone: str
    platform: Optional[InterviewPlatform]
    interviewers: list[InterviewerResponse]
