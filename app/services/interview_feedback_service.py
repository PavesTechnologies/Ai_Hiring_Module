from datetime import datetime, timezone
from uuid import UUID
from zoneinfo import ZoneInfo

from app.core.encryption_service import EncryptionService
from app.core.feedback_token import verify_feedback_token
from app.enums.constants import ActionType, EntityType
from app.exceptions.campaign_exceptions import CampaignException
from app.models.interview import InterviewFeedbackRecommendation
from app.repositories.CampaignRepository import CampaignRepository
from app.repositories.campaign_candidate_repository import CampaignCandidateRepository
from app.repositories.candidate_repository import CandidateRepository
from app.repositories.interview_feedback_repository import InterviewFeedbackRepository
from app.repositories.interview_schedule_repository import InterviewScheduleRepository
from app.schemas.interview_feedback_schema import (
    FeedbackFormContextResponse,
    InterviewFeedbackResponse,
    SubmitFeedbackRequest,
)
from app.services.audit_service import AuditService


class InterviewFeedbackService:
    """
    M12 Step 3 - interview feedback, advisory only: submitting it never
    touches interview_schedules.status or campaign_candidates.
    pipeline_stage - HR/HM still act via Epic 1's select/reject-interview
    endpoints. A round being "done" is signaled by feedback existing for
    it, not by any status field.

    Kept as its own service (not folded into InterviewScheduleService)
    because 2 of its 3 methods have a genuinely different access-control
    model - a signed, expiring token in the URL (interviewers have no
    user account at all), not JWT auth - and mixing that with
    InterviewScheduleService's authenticated-only methods would blur a
    security boundary that's clearer kept visibly separate.
    """

    def __init__(
        self,
        interview_feedback_repo: InterviewFeedbackRepository,
        interview_schedule_repo: InterviewScheduleRepository,
        campaign_candidate_repo: CampaignCandidateRepository,
        campaign_repo: CampaignRepository,
        candidate_repo: CandidateRepository,
        encryption_service: EncryptionService,
        audit_service: AuditService,
    ):
        self.interview_feedback_repo = interview_feedback_repo
        self.interview_schedule_repo = interview_schedule_repo
        self.campaign_candidate_repo = campaign_candidate_repo
        self.campaign_repo = campaign_repo
        self.candidate_repo = candidate_repo
        self.encryption_service = encryption_service
        self.audit_service = audit_service

    def _assert_hiring_manager_owns_campaign(self, campaign_candidate, user_id: str) -> None:
        campaign = self.campaign_repo.get_by_id(campaign_candidate.campaign_id)
        if not campaign:
            raise CampaignException("Campaign not found.", 404)
        if campaign.hiring_manager_id != user_id:
            raise CampaignException("You do not have access to this candidate's campaign.", 403)

    def _resolve_token(self, token: str) -> tuple:
        """
        Returns (schedule, interviewer). Raises a 404 CampaignException on
        any invalid/expired/tampered token, or on a token whose
        interviewer doesn't actually belong to the round it names -
        deliberately the same 404 either way (never reveal which part of
        the token was wrong to an unauthenticated caller).
        """
        try:
            interview_schedule_id, interviewer_id = verify_feedback_token(token)
        except ValueError:
            raise CampaignException("This feedback link is invalid or has expired.", 404)

        schedule = self.interview_schedule_repo.get_by_id(interview_schedule_id)
        interviewer = self.interview_schedule_repo.get_interviewer_by_id(interviewer_id)
        if schedule is None or interviewer is None or interviewer.interview_id != schedule.id:
            raise CampaignException("This feedback link is invalid or has expired.", 404)

        return schedule, interviewer

    def get_feedback_form_context(self, token: str) -> FeedbackFormContextResponse:
        schedule, interviewer = self._resolve_token(token)

        campaign_candidate = self.campaign_candidate_repo.get_by_id(schedule.campaign_candidate_id)
        candidate = self.candidate_repo.get_by_id(campaign_candidate.candidate_id)
        candidate_name = self.encryption_service.decrypt(candidate.full_name_encrypted, candidate.encryption_key_id)

        # Fix: the form-rendering GET previously gave no signal that this
        # token's feedback was already submitted - opening the same link
        # twice showed a fillable form both times, only failing (409) on
        # submit. Checked here, not just at submit time.
        existing_feedback = self.interview_feedback_repo.get_by_interview_schedule_id_and_interviewer_id(
            schedule.id, interviewer.id,
        )

        # Timezone-discrepancy fix: start_at/end_at are a real UTC instant
        # now - converted back to the round's own schedule.timezone here,
        # the same fix applied to notification emails, so the interviewer
        # sees the interview's actual intended local time, not raw UTC.
        local_start = schedule.start_at.astimezone(ZoneInfo(schedule.timezone)) if schedule.start_at else None
        local_end = schedule.end_at.astimezone(ZoneInfo(schedule.timezone)) if schedule.end_at else None

        return FeedbackFormContextResponse(
            candidate_name=candidate_name,
            interview_type=schedule.interview_type,
            round_number=schedule.round_number,
            date=local_start.date() if local_start else None,
            start_time=local_start.time() if local_start else None,
            end_time=local_end.time() if local_end else None,
            interviewer_name=interviewer.name,
            already_submitted=existing_feedback is not None,
            existing_recommendation=existing_feedback.recommendation if existing_feedback else None,
            existing_submitted_at=existing_feedback.submitted_at if existing_feedback else None,
        )

    def submit_feedback(self, token: str, request: SubmitFeedbackRequest) -> None:
        schedule, interviewer = self._resolve_token(token)

        # Fix: was previously safe by construction - the feedback link only
        # ever reached an interviewer via a reminder sent after the
        # interview's end_at had passed. That's no longer guaranteed to be
        # the only way the link reaches them, so this needs its own
        # explicit check. Same "end_at is None or in the future" shape as
        # InterviewScheduleService.complete()'s own "hasn't ended yet" guard.
        if schedule.end_at is None or schedule.end_at > datetime.now(timezone.utc):
            raise CampaignException("Feedback can't be submitted until after the interview has ended.", 400)

        feedback, was_created = self.interview_feedback_repo.create(
            schedule.id, interviewer.id, request.recommendation, request.notes,
        )
        if not was_created:
            self.interview_feedback_repo.rollback()
            raise CampaignException("Feedback has already been submitted for this interview.", 409)

        campaign_candidate = self.campaign_candidate_repo.get_by_id(schedule.campaign_candidate_id)
        self.audit_service.log(
            # No user account exists for an interviewer (deliberate - see
            # InterviewInterviewer) - actor_id is nullable specifically
            # for cases like this, same shape StageTransitionService.
            # transition() already uses for is_system writes, just a new
            # descriptive role for a genuinely new class of actor.
            actor_id=None,
            actor_role="EXTERNAL_INTERVIEWER",
            action_type=ActionType.INTERVIEW_FEEDBACK_SUBMITTED,
            entity_type=EntityType.CAMPAIGN_CANDIDATE,
            entity_id=campaign_candidate.id,
            campaign_id=campaign_candidate.campaign_id,
            details={
                "interview_schedule_id": str(schedule.id),
                "interviewer_id": str(interviewer.id),
                "recommendation": request.recommendation.value,
            },
        )
        self.interview_feedback_repo.commit()

    def get_feedback_for_round(
        self, campaign_candidate_id: UUID, interview_id: UUID, actor_id: str, actor_roles: list[str],
    ) -> list[InterviewFeedbackResponse]:
        campaign_candidate = self.campaign_candidate_repo.get_by_id(campaign_candidate_id)
        if campaign_candidate is None:
            raise CampaignException("Campaign candidate not found.", 404)
        if "HR_ADMIN" not in actor_roles:
            self._assert_hiring_manager_owns_campaign(campaign_candidate, actor_id)

        schedule = self.interview_schedule_repo.get_by_id(interview_id)
        if schedule is None or schedule.campaign_candidate_id != campaign_candidate_id:
            raise CampaignException("Interview not found.", 404)

        interviewers_by_id = {i.id: i for i in self.interview_schedule_repo.get_interviewers(interview_id)}
        feedback_rows = self.interview_feedback_repo.get_by_interview_schedule_id(interview_id)

        return [
            InterviewFeedbackResponse(
                id=row.id,
                interviewer_name=interviewers_by_id[row.interviewer_id].name,
                interviewer_email=interviewers_by_id[row.interviewer_id].email,
                recommendation=row.recommendation.value,
                notes=row.notes,
                submitted_at=row.submitted_at,
            )
            for row in feedback_rows
        ]
