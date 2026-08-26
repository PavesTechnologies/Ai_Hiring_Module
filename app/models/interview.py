import enum
import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    Boolean, DateTime, Enum as SAEnum, ForeignKey, Index, Integer, String, Text, UniqueConstraint, func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.database import Base

_USERS_FK = "users.id"


class InterviewStatus(enum.Enum):
    PENDING = "PENDING"
    SCHEDULED = "SCHEDULED"
    RESCHEDULED = "RESCHEDULED"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"


class InterviewPlatform(enum.Enum):
    TEAMS = "TEAMS"
    MEET = "MEET"
    ONSITE = "ONSITE"
    PHONE = "PHONE"


class InterviewHistoryEventType(enum.Enum):
    SCHEDULED = "SCHEDULED"
    RESCHEDULED = "RESCHEDULED"
    CANCELLED = "CANCELLED"


class InterviewFeedbackRecommendation(enum.Enum):
    ADVANCE = "ADVANCE"
    SELECT = "SELECT"
    REJECT = "REJECT"
    HOLD = "HOLD"


class InterviewSchedule(Base):
    """
    Epic 4 (M12) — one row per interview round for a campaign_candidate
    that has ever reached INTERVIEW. Round 1 is auto-created PENDING by
    StageTransitionService.transition()'s own INTERVIEW-entry hook (never
    created directly by these endpoints - schedule() only ever fills in
    an already-existing PENDING row for round 1); round 2+ are created
    explicitly by schedule() itself when called again against an already-
    SCHEDULED/RESCHEDULED/COMPLETED/CANCELLED latest round ("Schedule Next
    Round" - completes the current round and creates the next in one
    transaction).

    Multi-round redesign (M12 follow-up): campaign_candidate_id is
    deliberately NOT unique on its own anymore - UNIQUE(
    campaign_candidate_id, round_number) is the hard invariant now (a
    given round number for a given candidate can only ever exist once;
    the candidate itself can have many rounds).
    """
    __tablename__ = "interview_schedules"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    campaign_candidate_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("campaign_candidates.id"), nullable=False,
    )
    round_number: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[InterviewStatus] = mapped_column(
        SAEnum(InterviewStatus, name="interview_status_enum"), nullable=False, default=InterviewStatus.PENDING,
    )
    interview_type: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    start_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    end_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    # Timezone-discrepancy fix - the IANA zone (e.g. "Asia/Kolkata") the
    # round was actually scheduled in. start_at/end_at are always a real
    # UTC instant now; this is what lets that instant be converted back to
    # the originally-intended wall-clock time in the API response and in
    # notification emails, instead of every reader just echoing raw UTC
    # numbers. Default 'UTC' is a technical placeholder for rows that
    # predate this column - not a claim that they were genuinely scheduled
    # in UTC (there's no way to recover what they actually meant).
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="UTC")
    platform: Mapped[Optional[InterviewPlatform]] = mapped_column(
        SAEnum(InterviewPlatform, name="interview_platform_enum"), nullable=True,
    )
    meeting_link: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    location: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    cancel_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # M12 Microsoft Teams integration - the Graph calendar event's own id,
    # so reschedule/cancel know which event to PATCH/DELETE. Nullable: not
    # every interview has one (TEAMS-platform + a connected scheduler only;
    # everything else stays a manual-link fallback with no Graph event).
    external_calendar_event_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    scheduled_by: Mapped[Optional[str]] = mapped_column(String(255), ForeignKey(_USERS_FK), nullable=True)
    scheduled_by_role: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )

    __table_args__ = (
        UniqueConstraint(
            "campaign_candidate_id", "round_number",
            name="uq_interview_schedules_campaign_candidate_id_round_number",
        ),
    )


class InterviewInterviewer(Base):
    """
    Epic 4 (M12) — deliberately no user_id column: confirmed decision is
    no account resolution or creation for interviewer contacts, only
    storing exactly what the frontend sends (name/email as free text).
    """
    __tablename__ = "interview_interviewers"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    interview_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("interview_schedules.id"), nullable=False,
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    email: Mapped[str] = mapped_column(String(255), nullable=False)
    # Interviewer lifecycle follow-up - soft-remove, always, regardless of
    # whether the row is referenced by interview_feedback/email_notifications:
    # replace_interviewers() used to hard-delete an unreferenced removed row
    # and silently skip deleting a referenced one, leaving no way to tell
    # "removed" apart from "still on the round" in the latter case. Every FK
    # to this table stays valid either way - is_active is purely a "still on
    # the round going forward" flag, never touched by anything else.
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    __table_args__ = (
        Index("ix_interview_interviewers_interview_id", "interview_id"),
    )


class InterviewScheduleHistory(Base):
    """
    Epic 4 (M12) — append-only, matching campaign_candidate_stage_history's
    own guarantee: no update/delete path exists anywhere in this codebase
    for this table. Whether it needs the same DB-level immutability
    trigger Epic 3 Fix 4 gave audit_log is a real, separate follow-up
    decision - deliberately not applied in this migration.
    """
    __tablename__ = "interview_schedule_history"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    interview_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("interview_schedules.id"), nullable=False,
    )
    event_type: Mapped[InterviewHistoryEventType] = mapped_column(
        SAEnum(InterviewHistoryEventType, name="interview_history_event_type_enum"), nullable=False,
    )
    old_start_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    new_start_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    changed_by: Mapped[str] = mapped_column(String(255), ForeignKey(_USERS_FK), nullable=False)
    changed_by_role: Mapped[str] = mapped_column(String(50), nullable=False)
    reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        Index("ix_interview_schedule_history_interview_id_changed_at", "interview_id", "changed_at"),
    )


class InterviewFeedback(Base):
    """
    M12 Step 3 - one interviewer's feedback on one interview round.
    Advisory only: submitting this never touches interview_schedules.status
    or campaign_candidates.pipeline_stage - HR/HM still make the real call
    via Epic 1's select/reject-interview endpoints. A round being "done"
    is signaled by feedback existing for it, not by any status field.

    UNIQUE(interview_schedule_id, interviewer_id) is a deliberate hard
    lock, not an oversight: every other append-only guarantee in this
    system (interview_schedule_history, campaign_candidate_stage_history,
    audit_log's immutability trigger) exists so a submitted decision is
    never silently overwritten - a resubmission attempt is a 409, not an
    upsert. If a real correction need surfaces later, the answer is an
    explicit interview_feedback_history table mirroring the interview-
    schedule pattern, not a silent update path added now.

    Submitted by an interviewer with no user account (deliberate - see
    InterviewInterviewer) via a signed, expiring token
    (app/core/feedback_token.py), not authentication - there is no
    changed_by/actor_id column here for that reason; the audit_log entry
    for this action uses actor_id=None, actor_role="EXTERNAL_INTERVIEWER"
    instead (mirrors StageTransitionService.transition()'s existing
    is_system shape for a different kind of actor-less write).
    """
    __tablename__ = "interview_feedback"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    interview_schedule_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("interview_schedules.id"), nullable=False,
    )
    interviewer_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("interview_interviewers.id"), nullable=False,
    )
    recommendation: Mapped[InterviewFeedbackRecommendation] = mapped_column(
        SAEnum(InterviewFeedbackRecommendation, name="interview_feedback_recommendation_enum"), nullable=False,
    )
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    submitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "interview_schedule_id", "interviewer_id",
            name="uq_interview_feedback_interview_schedule_id_interviewer_id",
        ),
    )
