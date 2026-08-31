from datetime import date as date_type, datetime, time as time_type
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, Field

from app.models.interview import InterviewFeedbackRecommendation


class FeedbackFormContextResponse(BaseModel):
    """
    GET /interviews/feedback/{token} - just enough to render the form.
    Deliberately NOT the full candidate record (no email/phone/scores/
    other feedback) - an external interviewer with no account should see
    only what they need to give feedback, not internal candidate data.
    """
    candidate_name: str
    interview_type: Optional[str]
    round_number: int
    date: Optional[date_type]
    start_time: Optional[time_type]
    end_time: Optional[time_type]
    interviewer_name: str
    # Fix: GET previously gave no signal that this token's feedback was
    # already submitted - opening the same link twice showed a fillable
    # form both times, only failing (409) at the last step, on submit.
    # Sent alongside the normal form context (not instead of it) so the
    # frontend can choose to still show the round's read-only details on
    # the "already submitted" screen.
    already_submitted: bool = False
    existing_recommendation: Optional[InterviewFeedbackRecommendation] = None
    existing_submitted_at: Optional[datetime] = None


class SubmitFeedbackRequest(BaseModel):
    recommendation: InterviewFeedbackRecommendation
    notes: Optional[str] = Field(default=None, max_length=5000)


class InterviewFeedbackResponse(BaseModel):
    """GET .../feedback (authenticated) - one entry per interviewer who has submitted."""
    id: UUID
    interviewer_name: str
    interviewer_email: str
    recommendation: str
    notes: Optional[str]
    submitted_at: datetime
