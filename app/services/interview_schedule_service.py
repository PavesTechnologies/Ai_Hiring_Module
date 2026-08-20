from datetime import date, datetime, time, timezone
from uuid import UUID

from app.enums.constants import ActionType, EntityType
from app.exceptions.campaign_exceptions import CampaignException
from app.models.interview import InterviewHistoryEventType, InterviewPlatform, InterviewStatus
from app.repositories.CampaignRepository import CampaignRepository
from app.repositories.campaign_candidate_repository import CampaignCandidateRepository
from app.repositories.interview_schedule_repository import InterviewScheduleRepository
from app.schemas.interview_schema import (
    CancelInterviewRequest,
    InterviewerResponse,
    InterviewHistoryEntryResponse,
    InterviewScheduleResponse,
    RescheduleInterviewRequest,
    ScheduleInterviewRequest,
)
from app.services.audit_service import AuditService
from app.services.google_calendar_service import GoogleCalendarService
from app.services.microsoft_calendar_service import MicrosoftCalendarService


def _combine_utc(day: date, moment: time) -> datetime:
    return datetime.combine(day, moment, tzinfo=timezone.utc)


class InterviewScheduleService:
    """
    Epic 4 (M12) Step 3 - schedule/reschedule/cancel an interview_schedules
    row. Every row this service ever touches already exists: it was
    auto-created PENDING by StageTransitionService.transition()'s
    INTERVIEW-entry hook (see Step 2) - this service never inserts one.

    Ownership/role gating mirrors Epic 1's advance_to_interview/select/
    reject_at_interview exactly (HIRING_MANAGER must own the candidate's
    campaign, HR_ADMIN is exempt), resolved via campaign_candidate_id -
    reschedule/cancel are addressed by interview_id, so the candidate row
    is looked up through interview_schedule.campaign_candidate_id first.
    """

    def __init__(
        self,
        interview_schedule_repo: InterviewScheduleRepository,
        campaign_candidate_repo: CampaignCandidateRepository,
        campaign_repo: CampaignRepository,
        audit_service: AuditService,
        microsoft_calendar_service: MicrosoftCalendarService,
        google_calendar_service: GoogleCalendarService,
    ):
        self.interview_schedule_repo = interview_schedule_repo
        self.campaign_candidate_repo = campaign_candidate_repo
        self.campaign_repo = campaign_repo
        self.audit_service = audit_service
        # Both required, not optional-with-a-runtime-check (same
        # discipline as interview_schedule_repo on StageTransitionService)
        # - both are always safely constructible (no per-request state),
        # and their own internal logic already handles "user not
        # connected" as a normal outcome, not an error.
        self.microsoft_calendar_service = microsoft_calendar_service
        self.google_calendar_service = google_calendar_service

    def _calendar_service_for(self, platform: InterviewPlatform | None):
        """
        The single per-platform dispatch point - ONSITE/PHONE/None
        correctly resolve to None (no calendar call at all), matching
        today's implicit behavior. TEAMS/MEET resolve to a calendar
        service with identical create_event/update_event/delete_event
        signatures (see MicrosoftCalendarService/GoogleCalendarService's
        own docstrings for why they deliberately don't share a body
        builder despite the identical signatures), so callers below never
        need to branch on which provider they're talking to.
        """
        return {
            InterviewPlatform.TEAMS: self.microsoft_calendar_service,
            InterviewPlatform.MEET: self.google_calendar_service,
        }.get(platform)

    def _assert_hiring_manager_owns_campaign(self, campaign_candidate, user_id: str) -> None:
        campaign = self.campaign_repo.get_by_id(campaign_candidate.campaign_id)
        if not campaign:
            raise CampaignException("Campaign not found.", 404)
        if campaign.hiring_manager_id != user_id:
            raise CampaignException(
                "You do not have access to this candidate's campaign.", 403,
            )

    def _get_candidate_and_authorize(self, campaign_candidate_id: UUID, actor_id: str, actor_roles: list[str]):
        campaign_candidate = self.campaign_candidate_repo.get_by_id(campaign_candidate_id)
        if campaign_candidate is None:
            raise CampaignException("Campaign candidate not found.", 404)
        if "HR_ADMIN" not in actor_roles:
            self._assert_hiring_manager_owns_campaign(campaign_candidate, actor_id)
        return campaign_candidate

    def _to_response(self, schedule, interviewers, history) -> InterviewScheduleResponse:
        actor_ids = {schedule.scheduled_by, *(h.changed_by for h in history)}
        actor_ids.discard(None)
        names = self.campaign_repo.get_user_names(list(actor_ids)) if actor_ids else {}

        def _display_name(actor_id_value) -> str:
            if not actor_id_value:
                return "System"
            return names.get(str(actor_id_value), "System")

        duration_minutes = int((schedule.end_at - schedule.start_at).total_seconds() // 60)

        return InterviewScheduleResponse(
            id=schedule.id,
            campaign_candidate_id=schedule.campaign_candidate_id,
            status=schedule.status.value,
            interview_type=schedule.interview_type,
            interviewers=[
                InterviewerResponse(id=i.id, name=i.name, email=i.email) for i in interviewers
            ],
            date=schedule.start_at.date(),
            start_time=schedule.start_at.time(),
            end_time=schedule.end_at.time(),
            duration_minutes=duration_minutes,
            platform=schedule.platform,
            location=schedule.location,
            notes=schedule.notes,
            cancel_reason=schedule.cancel_reason,
            meeting_link=schedule.meeting_link,
            created_at=schedule.created_at,
            history=[
                InterviewHistoryEntryResponse(
                    id=h.id,
                    old_scheduled_at=h.old_start_at,
                    new_scheduled_at=h.new_start_at,
                    rescheduled_by=_display_name(h.changed_by),
                    reason=h.reason,
                    changed_at=h.changed_at,
                )
                for h in history
            ],
        )

    def schedule(
        self,
        campaign_candidate_id: UUID,
        actor_id: str,
        actor_roles: list[str],
        request: ScheduleInterviewRequest,
    ) -> InterviewScheduleResponse:
        campaign_candidate = self._get_candidate_and_authorize(campaign_candidate_id, actor_id, actor_roles)

        schedule = self.interview_schedule_repo.get_by_campaign_candidate_id(campaign_candidate_id)
        if schedule is None:
            raise CampaignException(
                "No interview_schedules row exists for this candidate - the candidate must "
                "reach the INTERVIEW pipeline stage before an interview can be scheduled.",
                409,
            )
        if schedule.status != InterviewStatus.PENDING:
            raise CampaignException(
                f"Interview is already {schedule.status.value} - use reschedule instead.", 409,
            )

        start_at = _combine_utc(request.date, request.start_time)
        end_at = _combine_utc(request.date, request.end_time)

        schedule.interview_type = request.interview_type
        schedule.start_at = start_at
        schedule.end_at = end_at
        schedule.platform = request.platform
        schedule.location = request.location
        schedule.notes = request.notes
        schedule.status = InterviewStatus.SCHEDULED
        schedule.scheduled_by = actor_id
        schedule.scheduled_by_role = actor_roles[0] if actor_roles else None
        schedule.updated_at = datetime.now(timezone.utc)

        attendees = [interviewer.model_dump() for interviewer in request.interviewers]
        calendar_service = self._calendar_service_for(request.platform)
        if calendar_service is not None:
            # Not-connected or any failure resolves to (None, None) -
            # never raises, never blocks scheduling (the manual-entry
            # fallback the scheduler pastes their own link into).
            schedule.meeting_link, schedule.external_calendar_event_id = calendar_service.create_event(
                actor_id, subject=request.interview_type or "Interview",
                start_at=start_at, end_at=end_at, attendees=attendees, location=request.location,
            )

        self.interview_schedule_repo.update(schedule)

        interviewers = self.interview_schedule_repo.replace_interviewers(schedule.id, attendees)

        self.audit_service.log(
            actor_id=actor_id,
            actor_role=actor_roles[0] if actor_roles else None,
            action_type=ActionType.INTERVIEW_SCHEDULED,
            entity_type=EntityType.CAMPAIGN_CANDIDATE,
            entity_id=campaign_candidate.id,
            campaign_id=campaign_candidate.campaign_id,
            details={
                "interview_id": str(schedule.id),
                "start_at": start_at.isoformat(),
                "end_at": end_at.isoformat(),
            },
        )

        self.interview_schedule_repo.commit()
        history = self.interview_schedule_repo.get_history(schedule.id)
        return self._to_response(schedule, interviewers, history)

    def reschedule(
        self,
        interview_id: UUID,
        actor_id: str,
        actor_roles: list[str],
        request: RescheduleInterviewRequest,
    ) -> InterviewScheduleResponse:
        schedule = self.interview_schedule_repo.get_by_id(interview_id)
        if schedule is None:
            raise CampaignException("Interview not found.", 404)

        self._get_candidate_and_authorize(schedule.campaign_candidate_id, actor_id, actor_roles)
        campaign_candidate = self.campaign_candidate_repo.get_by_id(schedule.campaign_candidate_id)

        if schedule.status not in (InterviewStatus.SCHEDULED, InterviewStatus.RESCHEDULED):
            raise CampaignException(
                f"Cannot reschedule an interview that is {schedule.status.value} - it must be "
                "SCHEDULED or RESCHEDULED first.",
                409,
            )

        old_start_at = schedule.start_at
        start_at = _combine_utc(request.date, request.start_time)
        end_at = _combine_utc(request.date, request.end_time)

        old_calendar_service = self._calendar_service_for(schedule.platform)
        existing_event_id = schedule.external_calendar_event_id

        schedule.interview_type = request.interview_type
        schedule.start_at = start_at
        schedule.end_at = end_at
        schedule.platform = request.platform
        schedule.location = request.location
        schedule.notes = request.notes
        schedule.status = InterviewStatus.RESCHEDULED
        schedule.updated_at = datetime.now(timezone.utc)

        attendees = [interviewer.model_dump() for interviewer in request.interviewers]
        new_calendar_service = self._calendar_service_for(request.platform)

        if new_calendar_service is not None:
            if new_calendar_service is old_calendar_service and existing_event_id:
                # Staying on the same video platform with an existing
                # event - PATCH it in place. Neither provider's update
                # call returns a new meeting link, so meeting_link is
                # left as whatever create_event originally produced.
                new_calendar_service.update_event(
                    actor_id, existing_event_id, subject=request.interview_type or "Interview",
                    start_at=start_at, end_at=end_at, attendees=attendees, location=request.location,
                )
            else:
                # Either switching TO a video platform from ONSITE/PHONE/
                # PENDING, or switching BETWEEN two different video
                # platforms (TEAMS<->MEET) - an event in one provider's
                # calendar can't be "updated" into the other provider's
                # calendar, so the old one (if any) is deleted and a new
                # one created on the new provider. Also covers the case
                # where the original create_event call failed and left no
                # existing_event_id at all - just creates one now.
                if old_calendar_service is not None and existing_event_id:
                    old_calendar_service.delete_event(actor_id, existing_event_id)
                schedule.meeting_link, schedule.external_calendar_event_id = new_calendar_service.create_event(
                    actor_id, subject=request.interview_type or "Interview",
                    start_at=start_at, end_at=end_at, attendees=attendees, location=request.location,
                )
        elif old_calendar_service is not None and existing_event_id:
            # Platform changed away from any video platform entirely -
            # the old meeting is no longer relevant to this interview.
            old_calendar_service.delete_event(actor_id, existing_event_id)
            schedule.meeting_link = None
            schedule.external_calendar_event_id = None

        self.interview_schedule_repo.update(schedule)

        interviewers = self.interview_schedule_repo.replace_interviewers(schedule.id, attendees)

        self.interview_schedule_repo.add_history(
            interview_id=schedule.id,
            event_type=InterviewHistoryEventType.RESCHEDULED,
            old_start_at=old_start_at,
            new_start_at=start_at,
            changed_by=actor_id,
            changed_by_role=actor_roles[0] if actor_roles else None,
            reason=request.reason,
        )

        self.audit_service.log(
            actor_id=actor_id,
            actor_role=actor_roles[0] if actor_roles else None,
            action_type=ActionType.INTERVIEW_RESCHEDULED,
            entity_type=EntityType.CAMPAIGN_CANDIDATE,
            entity_id=campaign_candidate.id,
            campaign_id=campaign_candidate.campaign_id,
            details={
                "interview_id": str(schedule.id),
                "old_start_at": old_start_at.isoformat() if old_start_at else None,
                "new_start_at": start_at.isoformat(),
                "reason": request.reason,
            },
        )

        self.interview_schedule_repo.commit()
        history = self.interview_schedule_repo.get_history(schedule.id)
        return self._to_response(schedule, interviewers, history)

    def cancel(
        self,
        interview_id: UUID,
        actor_id: str,
        actor_roles: list[str],
        request: CancelInterviewRequest,
    ) -> InterviewScheduleResponse:
        schedule = self.interview_schedule_repo.get_by_id(interview_id)
        if schedule is None:
            raise CampaignException("Interview not found.", 404)

        self._get_candidate_and_authorize(schedule.campaign_candidate_id, actor_id, actor_roles)
        campaign_candidate = self.campaign_candidate_repo.get_by_id(schedule.campaign_candidate_id)

        if schedule.status == InterviewStatus.CANCELLED:
            raise CampaignException("Interview is already cancelled.", 409)

        cancelled_start_at = schedule.start_at
        schedule.status = InterviewStatus.CANCELLED
        schedule.cancel_reason = request.reason
        schedule.updated_at = datetime.now(timezone.utc)

        calendar_service = self._calendar_service_for(schedule.platform)
        if calendar_service is not None and schedule.external_calendar_event_id:
            # Fail-safe, same as create/update - a failed delete must not
            # block cancellation in our system. external_calendar_event_id
            # is deliberately left on the row (not cleared) even on
            # success, matching "cancel_reason populated, notes untouched"
            # - cancel doesn't scrub fields beyond what it's actually
            # changing, and a non-null id on a CANCELLED row combined with
            # the log line above IS the manual-cleanup breadcrumb if the
            # delete itself failed.
            calendar_service.delete_event(actor_id, schedule.external_calendar_event_id)

        self.interview_schedule_repo.update(schedule)

        # interview_schedule_history exists specifically so a past decision's
        # context is never lost - a cancellation is exactly that kind of
        # decision, so it gets a row too, not just reschedule. new_start_at
        # is null (there is no new time), unlike a reschedule entry.
        self.interview_schedule_repo.add_history(
            interview_id=schedule.id,
            event_type=InterviewHistoryEventType.CANCELLED,
            old_start_at=cancelled_start_at,
            new_start_at=None,
            changed_by=actor_id,
            changed_by_role=actor_roles[0] if actor_roles else None,
            reason=request.reason,
        )

        self.audit_service.log(
            actor_id=actor_id,
            actor_role=actor_roles[0] if actor_roles else None,
            action_type=ActionType.INTERVIEW_CANCELLED,
            entity_type=EntityType.CAMPAIGN_CANDIDATE,
            entity_id=campaign_candidate.id,
            campaign_id=campaign_candidate.campaign_id,
            details={"interview_id": str(schedule.id), "cancel_reason": request.reason},
        )

        self.interview_schedule_repo.commit()
        interviewers = self.interview_schedule_repo.get_interviewers(schedule.id)
        history = self.interview_schedule_repo.get_history(schedule.id)
        return self._to_response(schedule, interviewers, history)
