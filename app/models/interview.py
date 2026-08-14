import enum
import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    DateTime, Enum as SAEnum, ForeignKey, Index, String, Text, UniqueConstraint, func,
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


class InterviewSchedule(Base):
    """
    Epic 4 (M12) — one row per campaign_candidate that has ever reached
    INTERVIEW, auto-created PENDING by StageTransitionService.transition()'s
    own INTERVIEW-entry hook (never created directly by these endpoints -
    schedule() only ever fills in an already-existing PENDING row).
    campaign_candidate_id is UNIQUE by design: this is a hard invariant,
    not a convention - a candidate re-entering INTERVIEW (e.g. after a
    fraud-review clear) must never get a second row.
    """
    __tablename__ = "interview_schedules"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    campaign_candidate_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("campaign_candidates.id"), nullable=False, unique=True,
    )
    status: Mapped[InterviewStatus] = mapped_column(
        SAEnum(InterviewStatus, name="interview_status_enum"), nullable=False, default=InterviewStatus.PENDING,
    )
    interview_type: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    start_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    end_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
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
