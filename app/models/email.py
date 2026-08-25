import enum
import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    Boolean, CheckConstraint, DateTime, Enum as SAEnum, ForeignKey, Index, String, Text, UniqueConstraint, func, text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.database import Base


class EmailTriggerEvent(enum.Enum):
    # Only value needed for M07-E03 S02 - additional trigger events (e.g.
    # interview scheduled) are a different epic and intentionally not
    # invented here.
    CANDIDATE_REJECTED = "CANDIDATE_REJECTED"
    # Epic 4 (M05-E04) Phase D0 - vocabulary only. Not yet sendable by any
    # code path (D11 - Persistent Failure Notification - doesn't exist
    # yet). D11 must also resolve a real schema mismatch before it can use
    # this: EmailNotification.candidate_id is a required, non-null FK to
    # candidates.id, but this trigger's recipients (the uploader + all
    # active HR_ADMIN) are internal staff, not a candidate - deliberately
    # left unresolved here, per explicit decision, until D11.
    UPLOAD_PERMANENTLY_FAILED = "UPLOAD_PERMANENTLY_FAILED"
    # M12 (Workflow & Interview Scheduling) - vocabulary only, matching the
    # UPLOAD_PERMANENTLY_FAILED precedent above: added here alongside the
    # Postgres-native enum migration (e686c750b7b4) so the two never drift.
    # Not yet sendable by any code path - no interview_schedules table or
    # send-email integration exists yet for these.
    INTERVIEW_SCHEDULED = "INTERVIEW_SCHEDULED"
    INTERVIEW_RESCHEDULED = "INTERVIEW_RESCHEDULED"
    INTERVIEW_CANCELLED = "INTERVIEW_CANCELLED"
    CANDIDATE_SELECTED = "CANDIDATE_SELECTED"
    # Epic 5 Step 4 - the first trigger event whose real recipient is an
    # EXTERNAL_INTERVIEWER row, not a candidate. Fired by a periodic
    # sweep once a round's end_at has passed (see
    # interview_feedback_request_sweep_service.py), not synchronously off
    # a user action like every trigger above it.
    INTERVIEW_FEEDBACK_REQUESTED = "INTERVIEW_FEEDBACK_REQUESTED"
    # Interviewer lifecycle follow-up - 3 more EXTERNAL_INTERVIEWER-recipient
    # trigger events, same recipient shape as INTERVIEW_FEEDBACK_REQUESTED
    # above. INVITATION is per-interviewer (like FEEDBACK_REQUESTED, deduped
    # per (interview_schedule_id, interviewer_id)); REMOVED fires when
    # replace_interviewers() sets is_active=false; CANCELLED is the
    # interviewer-facing counterpart to the candidate-facing
    # INTERVIEW_CANCELLED above - distinct trigger event since tone/content
    # differ, not reusing that one.
    INTERVIEW_INTERVIEWER_INVITATION = "INTERVIEW_INTERVIEWER_INVITATION"
    INTERVIEW_INTERVIEWER_REMOVED = "INTERVIEW_INTERVIEWER_REMOVED"
    INTERVIEW_INTERVIEWER_CANCELLED = "INTERVIEW_INTERVIEWER_CANCELLED"


class EmailNotificationStatus(enum.Enum):
    QUEUED = "QUEUED"
    SENT = "SENT"
    FAILED = "FAILED"


class EmailRecipientType(enum.Enum):
    # An existing platform candidate - the original, still-most-common
    # shape. candidate_id required, recipient_email/name/
    # interview_interviewer_id all NULL (see the CHECK constraint below).
    CANDIDATE = "CANDIDATE"
    # Epic 5 Step 2 - an external interview participant with no user
    # account at all (see InterviewInterviewer's own docstring for why).
    # candidate_id NULL, interview_interviewer_id + recipient_email/name
    # all required - recipient_email/name are a denormalized snapshot,
    # not just a live join, so a notification's own audit trail survives
    # even if the interviewer row is later edited (same reasoning as
    # InterviewScheduleHistory snapshotting rather than pointing at
    # current state).
    EXTERNAL_INTERVIEWER = "EXTERNAL_INTERVIEWER"


class EmailTemplate(Base):
    __tablename__ = "email_templates"
    __table_args__ = (
        # Same "one active row per key" pattern already used for
        # JobDescription.uq_jd_active_lineage_version - a partial unique
        # index, not a plain UniqueConstraint, since multiple INACTIVE
        # templates for the same trigger_event (draft/superseded versions)
        # must still be allowed.
        Index(
            "uq_email_templates_active_trigger_event",
            "trigger_event",
            unique=True,
            postgresql_where=text("is_active = true"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    trigger_event: Mapped[EmailTriggerEvent] = mapped_column(
        SAEnum(EmailTriggerEvent, name="email_trigger_event_enum"), nullable=False,
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    subject: Mapped[str] = mapped_column(String(255), nullable=False)
    body_template: Mapped[str] = mapped_column(Text, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class EmailNotification(Base):
    """
    Epic 5 Step 2 - candidate_id is nullable (was NOT NULL through M12):
    recipient_type + the CHECK constraint below are what actually
    determine which recipient shape a given row has now, not the
    presence/absence of candidate_id alone. See email_recipient_model
    widening migration (ff1c2b57fbaf) for the full reasoning on why this
    is one wider table rather than two narrower ones.
    """
    __tablename__ = "email_notifications"
    __table_args__ = (
        CheckConstraint(
            "(recipient_type = 'CANDIDATE' AND candidate_id IS NOT NULL "
            "AND interview_interviewer_id IS NULL AND recipient_email IS NULL AND recipient_name IS NULL) "
            "OR "
            "(recipient_type = 'EXTERNAL_INTERVIEWER' AND candidate_id IS NULL "
            "AND interview_interviewer_id IS NOT NULL AND recipient_email IS NOT NULL AND recipient_name IS NOT NULL)",
            name="chk_email_notifications_recipient_shape",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    candidate_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("candidates.id"), nullable=True)
    # Optional: which rejection triggered this, for traceability - not in
    # the ticket's explicit field list but nullable so it never blocks a
    # future trigger_event that isn't campaign_candidate-scoped.
    campaign_candidate_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("campaign_candidates.id"), nullable=True,
    )
    trigger_event: Mapped[EmailTriggerEvent] = mapped_column(
        SAEnum(EmailTriggerEvent, name="email_trigger_event_enum"), nullable=False,
    )
    template_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("email_templates.id"), nullable=False)
    status: Mapped[EmailNotificationStatus] = mapped_column(
        SAEnum(EmailNotificationStatus, name="email_notification_status_enum"),
        nullable=False, default=EmailNotificationStatus.QUEUED,
    )
    sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    # T03: populated only on the terminal FAILED path.
    error_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    recipient_type: Mapped[EmailRecipientType] = mapped_column(
        SAEnum(EmailRecipientType, name="email_recipient_type_enum"),
        nullable=False, default=EmailRecipientType.CANDIDATE,
    )
    interview_interviewer_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("interview_interviewers.id"), nullable=True,
    )
    recipient_email: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    recipient_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    # Point-in-time display values (interview_date/time/mode/
    # interviewer_name, ...) snapshotted at queue time - deliberately NOT
    # re-resolved live at send time the way job_title/candidate_name are.
    # See ff1c2b57fbaf's docstring for the race this avoids.
    template_context: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    # Epic 5 Step 2 - which round this notification is about, when it's
    # about one at all (NULL for CANDIDATE_REJECTED/CANDIDATE_SELECTED).
    # Exists specifically so INTERVIEW_SCHEDULED/RESCHEDULED/CANCELLED can
    # dedup per-round instead of per-candidate - see
    # candidate_notification_emails.py.
    interview_schedule_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("interview_schedules.id"), nullable=True,
    )


class UserNotificationPreference(Base):
    """
    Epic 5 Step 3 - minimal, opt-out preference: an unlisted (user_id,
    trigger_event) pair means enabled, so this table only ever needs rows
    for explicit opt-outs (is_enabled=False in practice). Built ahead of
    any real caller - of the 6 real EmailTriggerEvent values, the 5 with
    a live send path today all target a candidate or external
    interviewer, neither of which has a users.id row to hold a
    preference against; UPLOAD_PERMANENTLY_FAILED is the one trigger
    actually scoped for internal users but has no send path of its own
    yet (D11, unbuilt). See is_notification_enabled() in
    app/services/notifications/user_notification_preferences.py and
    docs/known_issues.md's entry naming this "built ahead of need" shape.
    """
    __tablename__ = "user_notification_preferences"
    __table_args__ = (
        UniqueConstraint("user_id", "trigger_event", name="uq_user_notification_preferences_user_id_trigger_event"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[str] = mapped_column(String(255), ForeignKey("users.id"), nullable=False)
    trigger_event: Mapped[EmailTriggerEvent] = mapped_column(
        SAEnum(EmailTriggerEvent, name="email_trigger_event_enum"), nullable=False,
    )
    is_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False,
    )
