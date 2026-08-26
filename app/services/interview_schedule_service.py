from datetime import date, datetime, time, timezone
from uuid import UUID
from zoneinfo import ZoneInfo

from app.core.encryption_service import EncryptionService
from app.enums.constants import ActionType, EntityType
from app.models.interview import InterviewHistoryEventType, InterviewPlatform, InterviewStatus
from app.exceptions.campaign_exceptions import CampaignException
from app.repositories.CampaignRepository import CampaignRepository
from app.repositories.campaign_candidate_repository import CampaignCandidateRepository
from app.repositories.dashboard_repository import DashboardRepository
from app.repositories.encryption_key_repository import EncryptionKeyRepository
from app.repositories.interview_feedback_repository import InterviewFeedbackRepository
from app.repositories.interview_schedule_repository import InterviewScheduleRepository
from app.schemas.interview_schema import (
    CampaignInterviewEntry,
    CancelInterviewRequest,
    CompleteInterviewResponse,
    InterviewerResponse,
    InterviewHistoryEntryResponse,
    InterviewScheduleResponse,
    RequestFeedbackResponse,
    RescheduleInterviewRequest,
    ScheduleInterviewRequest,
)
from app.services.audit_service import AuditService
from app.services.google_calendar_service import GoogleCalendarService
from app.services.microsoft_calendar_service import MicrosoftCalendarService
from app.services.notifications.candidate_notification_emails import (
    queue_interview_cancelled_email,
    queue_interview_rescheduled_email,
    queue_interview_scheduled_email,
)
from app.services.notifications.interview_feedback_request_emails import queue_pending_feedback_requests_for_round
from app.services.notifications.interview_interviewer_lifecycle_emails import (
    queue_interview_interviewer_cancelled_email,
    queue_interview_interviewer_invitation_email,
    queue_interview_interviewer_removed_email,
)


def _combine_to_utc(day: date, moment: time, tz_name: str) -> datetime:
    """
    Timezone-discrepancy fix: a real conversion, not a relabel. The
    previous _combine_utc() just tagged the client's raw date/time with
    tzinfo=UTC regardless of what zone the client actually meant - this
    builds the wall-clock instant in the ROUND'S OWN declared zone first,
    then converts it to a genuine UTC instant for storage.
    """
    local = datetime.combine(day, moment, tzinfo=ZoneInfo(tz_name))
    return local.astimezone(timezone.utc)


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
        interview_feedback_repo: InterviewFeedbackRepository,
    ):
        self.interview_schedule_repo = interview_schedule_repo
        self.campaign_candidate_repo = campaign_candidate_repo
        self.campaign_repo = campaign_repo
        self.audit_service = audit_service
        # Required, not optional-with-a-runtime-check - same discipline
        # as every other collaborator on this class. Only needed by
        # request_feedback() (the manual "Request Feedback" trigger),
        # but every real caller already has a db session to build one.
        self.interview_feedback_repo = interview_feedback_repo
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

        # PENDING rows (reached INTERVIEW, nothing scheduled yet) have
        # start_at/end_at still null - only reachable via get_current()
        # today, since schedule/reschedule/cancel all run after these are
        # already set, but _to_response must not assume that forever.
        has_time = schedule.start_at is not None and schedule.end_at is not None
        duration_minutes = (
            int((schedule.end_at - schedule.start_at).total_seconds() // 60) if has_time else None
        )

        # Timezone-discrepancy fix: start_at/end_at are stored as a real
        # UTC instant - converting back to schedule.timezone here (not
        # raw UTC) means the date/start_time/end_time this response
        # returns match what was actually typed into schedule()/
        # reschedule(), not a UTC-shifted version of it.
        local_start = schedule.start_at.astimezone(ZoneInfo(schedule.timezone)) if has_time else None
        local_end = schedule.end_at.astimezone(ZoneInfo(schedule.timezone)) if has_time else None

        return InterviewScheduleResponse(
            id=schedule.id,
            campaign_candidate_id=schedule.campaign_candidate_id,
            round_number=schedule.round_number,
            status=schedule.status.value,
            interview_type=schedule.interview_type,
            interviewers=[
                InterviewerResponse(id=i.id, name=i.name, email=i.email) for i in interviewers
            ],
            date=local_start.date() if has_time else None,
            start_time=local_start.time() if has_time else None,
            end_time=local_end.time() if has_time else None,
            timezone=schedule.timezone if has_time else None,
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

    def get_rounds(
        self,
        campaign_candidate_id: UUID,
        actor_id: str,
        actor_roles: list[str],
    ) -> list[InterviewScheduleResponse]:
        """
        Read-only - GET .../interviews. Multi-round follow-up: a candidate
        can now have several interview_schedules rows (one per round),
        UNIQUE(campaign_candidate_id, round_number) rather than a single
        UNIQUE(campaign_candidate_id) - returns all of them, ordered by
        round_number ascending, as a plain list (matching every other
        list-returning endpoint's APIResponse[list[...]] convention in
        this codebase - no extra {"rounds": [...]} wrapper). Formerly
        get_current(), returning one resource; renamed since "current"
        stops making sense once there can be several.
        """
        self._get_candidate_and_authorize(campaign_candidate_id, actor_id, actor_roles)

        rounds = self.interview_schedule_repo.get_all_by_campaign_candidate_id(campaign_candidate_id)
        if not rounds:
            raise CampaignException(
                "No interview_schedules row exists for this candidate - the candidate must "
                "reach the INTERVIEW pipeline stage first.",
                404,
            )

        responses = []
        for schedule in rounds:
            interviewers = self.interview_schedule_repo.get_active_interviewers(schedule.id)
            history = self.interview_schedule_repo.get_history(schedule.id)
            responses.append(self._to_response(schedule, interviewers, history))
        return responses

    def _authorize_campaign_access(self, campaign, actor_id: str, actor_roles: list[str]) -> None:
        """
        Campaign-wide interview calendar follow-up - the first campaign-
        level (not candidate-level) ownership check in this codebase.
        HIRING_MANAGER mirrors the candidate-level
        campaign.hiring_manager_id == user_id check used everywhere else
        in this file; RECRUITER reuses DashboardRepository's established
        "campaigns I uploaded to, bulk-uploaded to, or created" definition
        - HiringCampaign.recruiter_id exists on the model but is never
        actually checked against the acting user anywhere in this
        codebase, so it's deliberately NOT what's used here.
        """
        if "HR_ADMIN" in actor_roles:
            return
        if "HIRING_MANAGER" in actor_roles and campaign.hiring_manager_id == actor_id:
            return
        if "RECRUITER" in actor_roles:
            dashboard_repo = DashboardRepository(self.campaign_candidate_repo.db)
            if dashboard_repo.is_campaign_accessible_to_recruiter(actor_id, campaign.id):
                return
        raise CampaignException("You do not have access to this campaign's interviews.", 403)

    def get_campaign_interviews(
        self,
        campaign_id: UUID,
        actor_id: str,
        actor_roles: list[str],
        start_date: date | None = None,
        end_date: date | None = None,
        statuses: list[InterviewStatus] | None = None,
        interviewer_email: str | None = None,
    ) -> list[CampaignInterviewEntry]:
        """
        Campaign-wide interview calendar - GET /campaigns/{campaign_id}/
        interviews. The first query in this codebase joining
        interview_schedules across every candidate in a campaign (every
        other interview endpoint is scoped to one campaign_candidate_id).
        No pagination - matches the established "whole campaign view"
        convention (board/stalled-candidates/rejection-analytics/
        pipeline-summary all return the full computed result), not
        upload-history's row-list-style limit/offset - the frontend's own
        date-range request is expected to bound this in practice.
        """
        campaign = self.campaign_repo.get_by_id(campaign_id)
        if campaign is None:
            raise CampaignException("Campaign not found.", 404)
        self._authorize_campaign_access(campaign, actor_id, actor_roles)

        start_at_after = datetime.combine(start_date, time.min, tzinfo=timezone.utc) if start_date else None
        start_at_before = datetime.combine(end_date, time.max, tzinfo=timezone.utc) if end_date else None

        schedules = self.interview_schedule_repo.get_by_campaign_id(
            campaign_id,
            start_at_after=start_at_after, start_at_before=start_at_before,
            statuses=statuses, interviewer_email=interviewer_email,
        )
        if not schedules:
            return []

        interviewers_by_interview_id: dict[UUID, list] = {}
        for interviewer in self.interview_schedule_repo.get_active_interviewers_by_interview_ids(
            [schedule.id for schedule in schedules],
        ):
            interviewers_by_interview_id.setdefault(interviewer.interview_id, []).append(interviewer)

        # Reuses the campaign board's own decrypt-per-row convention
        # (CampaignCandidateService._decrypt_candidate_name) - no email
        # exposed, full decrypted name only. EncryptionService is
        # constructed ad-hoc here rather than added to this class's
        # constructor, matching the ad-hoc-repo-from-an-existing-db-handle
        # convention already used elsewhere in this codebase for an
        # occasional-use dependency (e.g. candidate_notification_emails.py).
        encryption_service = EncryptionService(EncryptionKeyRepository(self.campaign_candidate_repo.db))
        candidate_names_by_campaign_candidate_id: dict[UUID, str] = {}
        for campaign_candidate, candidate, _resume in self.campaign_candidate_repo.get_all_by_campaign(campaign_id):
            if candidate is not None:
                candidate_names_by_campaign_candidate_id[campaign_candidate.id] = encryption_service.decrypt(
                    candidate.full_name_encrypted, candidate.encryption_key_id,
                )

        return [
            CampaignInterviewEntry(
                id=schedule.id,
                campaign_candidate_id=schedule.campaign_candidate_id,
                candidate_name=candidate_names_by_campaign_candidate_id.get(schedule.campaign_candidate_id, "Unknown"),
                round_number=schedule.round_number,
                interview_type=schedule.interview_type,
                status=schedule.status.value,
                start_at=schedule.start_at,
                end_at=schedule.end_at,
                timezone=schedule.timezone,
                platform=schedule.platform,
                interviewers=[
                    InterviewerResponse(id=i.id, name=i.name, email=i.email)
                    for i in interviewers_by_interview_id.get(schedule.id, [])
                ],
            )
            for schedule in schedules
        ]

    def _queue_interviewer_lifecycle_emails(self, campaign_candidate, schedule, active_interviewers, removed_interviewers) -> None:
        """
        Interviewer lifecycle follow-up - called after schedule()/
        reschedule() commit. Invitation is queued for every currently-
        active interviewer (not just ones newly added this call) since
        the per-(interview_schedule_id, interviewer_id) dedup inside
        queue_interview_interviewer_invitation_email is what actually
        decides "already invited" - this lets the caller pass the same
        active list on every schedule()/reschedule() call without
        tracking "who's genuinely new" itself, and a genuinely new
        interviewer added on a later reschedule still gets their first
        invitation correctly.
        """
        db = self.campaign_candidate_repo.db
        for interviewer in active_interviewers:
            queue_interview_interviewer_invitation_email(db, campaign_candidate, schedule, interviewer)
        for interviewer in removed_interviewers:
            queue_interview_interviewer_removed_email(db, campaign_candidate, schedule, interviewer)

    def _apply_schedule_request(
        self, schedule, request: ScheduleInterviewRequest, actor_id: str, actor_roles: list[str],
    ) -> list[dict]:
        """
        Fills in a PENDING-shaped row (fresh fields, status->SCHEDULED,
        calendar event if applicable) - shared by round 1 (the hook-
        created PENDING row) and every round 2+ reservation from
        create_next_round, since both are "a blank round getting its
        first real schedule() call" from this method's point of view.
        Returns the attendee dicts so the caller can pass them to
        replace_interviewers without rebuilding them.
        """
        start_at = _combine_to_utc(request.date, request.start_time, request.timezone)
        end_at = _combine_to_utc(request.date, request.end_time, request.timezone)

        schedule.interview_type = request.interview_type
        schedule.start_at = start_at
        schedule.end_at = end_at
        schedule.timezone = request.timezone
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
        return attendees

    def schedule(
        self,
        campaign_candidate_id: UUID,
        actor_id: str,
        actor_roles: list[str],
        request: ScheduleInterviewRequest,
    ) -> InterviewScheduleResponse:
        """
        Multi-round follow-up: round-aware. The candidate's *latest* round
        (highest round_number, not "the" row - there can be several) is
        what decides what this call means:

        - No row at all: 409 - the candidate hasn't reached INTERVIEW /
          predates the auto-create hook and wasn't backfilled.
        - Latest round PENDING (round 1, fresh from the hook): fill it in
          exactly as before - this is the very first schedule for this
          candidate.
        - Latest round SCHEDULED/RESCHEDULED/CANCELLED: "Schedule Next
          Round" - creates round_number+1 with the freshly submitted
          request body. The previous round's own status is never touched
          here (see the cascading-cancellation hook in
          StageTransitionService.transition()/PipelineTransitionService.
          transition_stage()/CampaignService.override_candidate_stage()
          for how a round actually gets closed out - not by scheduling
          the next one). status only ever tracks this round's own
          logistics now (PENDING/SCHEDULED/RESCHEDULED/CANCELLED); a
          round being "done" is signaled by feedback existing for it
          (a later step), not by this field.
        """
        campaign_candidate = self._get_candidate_and_authorize(campaign_candidate_id, actor_id, actor_roles)

        latest = self.interview_schedule_repo.get_latest_by_campaign_candidate_id(campaign_candidate_id)
        if latest is None:
            raise CampaignException(
                "No interview_schedules row exists for this candidate - the candidate must "
                "reach the INTERVIEW pipeline stage before an interview can be scheduled.",
                409,
            )

        previous_round = None
        if latest.status == InterviewStatus.PENDING:
            schedule = latest
        else:
            previous_round = latest

            schedule, was_created = self.interview_schedule_repo.create_next_round(
                campaign_candidate_id, latest.round_number + 1,
            )
            if not was_created:
                # Lost the race to a concurrent "Schedule Next Round" call
                # for this same candidate - the winner already filled in
                # this round's fields/interviewers/calendar event. Return
                # its current state as-is; do not touch any of it.
                self.interview_schedule_repo.commit()
                interviewers = self.interview_schedule_repo.get_active_interviewers(schedule.id)
                history = self.interview_schedule_repo.get_history(schedule.id)
                return self._to_response(schedule, interviewers, history)

        attendees = self._apply_schedule_request(schedule, request, actor_id, actor_roles)
        self.interview_schedule_repo.update(schedule)
        interviewers, removed_interviewers = self.interview_schedule_repo.replace_interviewers(schedule.id, attendees)

        details = {
            "interview_id": str(schedule.id),
            "round_number": schedule.round_number,
            "start_at": schedule.start_at.isoformat(),
            "end_at": schedule.end_at.isoformat(),
        }
        if previous_round is not None:
            details["previous_round_id"] = str(previous_round.id)
            details["previous_round_number"] = previous_round.round_number

        self.audit_service.log(
            actor_id=actor_id,
            actor_role=actor_roles[0] if actor_roles else None,
            action_type=ActionType.INTERVIEW_SCHEDULED,
            entity_type=EntityType.CAMPAIGN_CANDIDATE,
            entity_id=campaign_candidate.id,
            campaign_id=campaign_candidate.campaign_id,
            details=details,
        )

        self.interview_schedule_repo.commit()
        queue_interview_scheduled_email(self.campaign_candidate_repo.db, campaign_candidate, schedule, interviewers)
        self._queue_interviewer_lifecycle_emails(campaign_candidate, schedule, interviewers, removed_interviewers)
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

        # Epic 5 follow-up: a distinct, unambiguous message for COMPLETED
        # specifically - it's a terminal state (the interview already
        # happened, feedback may already be requested/given), not just
        # "wrong status, try one of these instead" the way the generic
        # message below reads. Checked before the broader status check
        # so COMPLETED never falls through to that more general wording.
        if schedule.status == InterviewStatus.COMPLETED:
            raise CampaignException("This interview is marked complete and can no longer be edited.", 409)

        if schedule.status not in (InterviewStatus.SCHEDULED, InterviewStatus.RESCHEDULED, InterviewStatus.CANCELLED):
            raise CampaignException(
                f"Cannot reschedule an interview that is {schedule.status.value} - it must be "
                "SCHEDULED, RESCHEDULED, or CANCELLED first.",
                409,
            )

        was_cancelled = schedule.status == InterviewStatus.CANCELLED

        old_start_at = schedule.start_at
        old_end_at = schedule.end_at
        start_at = _combine_to_utc(request.date, request.start_time, request.timezone)
        end_at = _combine_to_utc(request.date, request.end_time, request.timezone)

        # Epic 5 follow-up: an interviewer-only (or platform/location/
        # notes-only) edit with the SAME date/time is a quiet edit, not a
        # reschedule event - it must not flip status, write a RESCHEDULED
        # history/audit entry, or tell the candidate their interview
        # moved when it didn't (queue_interview_rescheduled_email).
        # Reactivating from CANCELLED always counts as a reschedule event
        # regardless of whether the new time happens to numerically match
        # the old one - the round wasn't logistically active before at
        # all, so this is a genuine (re)scheduling event from both the
        # candidate's and the system's perspective, not a no-op. Calendar
        # sync below is deliberately NOT gated on this - Teams/Google's
        # attendee list has to stay accurate regardless of whether this
        # counts as a "reschedule" in our own history/audit/notification
        # sense.
        is_reschedule_event = was_cancelled or start_at != old_start_at or end_at != old_end_at

        old_calendar_service = self._calendar_service_for(schedule.platform)
        existing_event_id = schedule.external_calendar_event_id

        if was_cancelled:
            # cancel() already deleted (or tried to delete) whatever
            # calendar event existed - reactivating must never try to
            # update, or re-delete, a provider-side event that's already
            # gone. Treat this exactly like a fresh schedule for calendar
            # purposes, and clear the stale cancel_reason/meeting_link/
            # external_calendar_event_id left over from the cancellation
            # (external_calendar_event_id was deliberately kept on a
            # CANCELLED row as a manual-cleanup breadcrumb - that job is
            # done once this row is active again).
            old_calendar_service = None
            existing_event_id = None
            schedule.meeting_link = None
            schedule.external_calendar_event_id = None
            schedule.cancel_reason = None

        schedule.interview_type = request.interview_type
        schedule.start_at = start_at
        schedule.end_at = end_at
        schedule.timezone = request.timezone
        schedule.platform = request.platform
        schedule.location = request.location
        schedule.notes = request.notes
        if is_reschedule_event:
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

        interviewers, removed_interviewers = self.interview_schedule_repo.replace_interviewers(schedule.id, attendees)

        if is_reschedule_event:
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
        if is_reschedule_event:
            queue_interview_rescheduled_email(self.campaign_candidate_repo.db, campaign_candidate, schedule, interviewers)
        self._queue_interviewer_lifecycle_emails(campaign_candidate, schedule, interviewers, removed_interviewers)
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
        interviewers = self.interview_schedule_repo.get_active_interviewers(schedule.id)
        queue_interview_cancelled_email(self.campaign_candidate_repo.db, campaign_candidate, schedule, interviewers)
        # Interviewer lifecycle follow-up - cancel() previously notified
        # only the candidate. Every still-active interviewer on the round
        # gets their own distinct notice (tone/content differ from the
        # candidate-facing email) - a since-removed interviewer is not
        # involved anymore and correctly gets nothing here.
        for interviewer in interviewers:
            queue_interview_interviewer_cancelled_email(
                self.campaign_candidate_repo.db, campaign_candidate, schedule, interviewer, request.reason,
            )
        history = self.interview_schedule_repo.get_history(schedule.id)
        return self._to_response(schedule, interviewers, history)

    def request_feedback(self, interview_id: UUID, actor_id: str, actor_roles: list[str]) -> RequestFeedbackResponse:
        """
        Epic 5 - manual counterpart to InterviewFeedbackRequestSweepService's
        hourly sweep (Step 4): lets HR/HM trigger INTERVIEW_FEEDBACK_REQUESTED
        immediately instead of waiting for end_at to be swept. "Who still
        needs asking" is resolved through the exact same
        queue_pending_feedback_requests_for_round()/
        queue_interview_feedback_requested_email() the sweep uses, so the
        same per-interviewer dedup guarantee holds regardless of which
        path fires first - clicking this right after the sweep ran (or
        vice versa) never double-sends.

        A round with nothing left to request (every interviewer already
        gave feedback or already has a pending email) is not an error -
        queued_count=0 is a valid, successful result; the button being
        clickable when there's nothing left to do is a frontend concern.

        status is restricted to SCHEDULED/RESCHEDULED, mirroring
        get_ended_active_rounds' own filter exactly - without this, a
        CANCELLED round would still pass the start_at check below
        whenever it was cancelled after its original start_at already
        passed (cancel() never clears start_at), letting this endpoint
        request feedback for an interview that never actually happened.
        The sweep never has this problem since its query never considers
        CANCELLED rounds in the first place; this check keeps the two
        paths from diverging on what counts as a valid target.
        """
        schedule = self.interview_schedule_repo.get_by_id(interview_id)
        if schedule is None:
            raise CampaignException("Interview not found.", 404)

        campaign_candidate = self._get_candidate_and_authorize(schedule.campaign_candidate_id, actor_id, actor_roles)

        if schedule.status not in (InterviewStatus.SCHEDULED, InterviewStatus.RESCHEDULED):
            raise CampaignException(
                f"Cannot request feedback for an interview with status {schedule.status.value}.", 400,
            )

        if schedule.start_at is None or schedule.start_at > datetime.now(timezone.utc):
            raise CampaignException("Cannot request feedback before the interview has started.", 400)

        interviewers = self.interview_schedule_repo.get_active_interviewers(schedule.id)
        queued_count = queue_pending_feedback_requests_for_round(
            self.campaign_candidate_repo.db, campaign_candidate, schedule, interviewers, self.interview_feedback_repo,
        )
        return RequestFeedbackResponse(queued_count=queued_count)

    def complete(self, interview_id: UUID, actor_id: str, actor_roles: list[str]) -> CompleteInterviewResponse:
        """
        Epic 5 follow-up - "Mark as Completed": manually flips a round to
        COMPLETED once its interview has actually happened, and fires the
        same feedback-request queueing as the sweep and the manual
        "Request Feedback" button - a third caller of
        queue_pending_feedback_requests_for_round(), so all three paths
        share the exact same "who still needs asking" logic and can never
        diverge or double-send.

        status flips and commits first; feedback queueing is a best-
        effort follow-up after, matching every other notification hook in
        this codebase (see candidate_notification_emails.py) - a failure
        to queue must never undo the completion that already committed.
        """
        schedule = self.interview_schedule_repo.get_by_id(interview_id)
        if schedule is None:
            raise CampaignException("Interview not found.", 404)

        campaign_candidate = self._get_candidate_and_authorize(schedule.campaign_candidate_id, actor_id, actor_roles)

        if schedule.status in (InterviewStatus.COMPLETED, InterviewStatus.CANCELLED):
            raise CampaignException(f"Cannot mark an interview complete - it is already {schedule.status.value}.", 400)

        if schedule.end_at is None or schedule.end_at > datetime.now(timezone.utc):
            raise CampaignException("Cannot mark an interview complete before it has ended.", 400)

        schedule.status = InterviewStatus.COMPLETED
        schedule.updated_at = datetime.now(timezone.utc)
        self.interview_schedule_repo.update(schedule)
        self.interview_schedule_repo.commit()

        interviewers = self.interview_schedule_repo.get_active_interviewers(schedule.id)
        feedback_queued_count = queue_pending_feedback_requests_for_round(
            self.campaign_candidate_repo.db, campaign_candidate, schedule, interviewers, self.interview_feedback_repo,
        )
        return CompleteInterviewResponse(status=schedule.status.value, feedback_queued_count=feedback_queued_count)
