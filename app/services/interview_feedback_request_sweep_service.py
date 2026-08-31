from datetime import datetime, timezone

from app.repositories.campaign_candidate_repository import CampaignCandidateRepository
from app.repositories.interview_feedback_repository import InterviewFeedbackRepository
from app.repositories.interview_schedule_repository import InterviewScheduleRepository
from app.services.notifications.interview_feedback_request_emails import queue_pending_feedback_requests_for_round


class InterviewFeedbackRequestSweepService:
    """
    Epic 5 Step 4 - the hourly sweep behind INTERVIEW_FEEDBACK_REQUESTED.
    Matches this codebase's one real precedent for "act once the clock
    passes X" logic (CampaignSchedulerService.detect_stalled_candidate_
    alerts) rather than a per-round Celery ETA task - see this feature's
    own Step 0/1 investigation for why (no ETA-scheduling precedent
    exists anywhere in this codebase, and a periodic sweep naturally
    handles reschedule/cancel with no revocation logic needed: a
    cancelled or rescheduled-to-the-future round simply stops matching
    get_ended_active_rounds' end_at < now() filter).

    A manual counterpart exists too - InterviewScheduleService.
    request_feedback() lets HR/HM trigger the same email immediately
    instead of waiting for this sweep. Both resolve "who still needs
    asking" through the same queue_pending_feedback_requests_for_round(),
    so triggering one right after the other never double-sends.
    """

    def __init__(
        self,
        db,
        interview_schedule_repo: InterviewScheduleRepository,
        interview_feedback_repo: InterviewFeedbackRepository,
        campaign_candidate_repo: CampaignCandidateRepository,
    ):
        self.db = db
        self.interview_schedule_repo = interview_schedule_repo
        self.interview_feedback_repo = interview_feedback_repo
        self.campaign_candidate_repo = campaign_candidate_repo

    def run(self) -> int:
        """Returns the number of feedback-request emails actually queued (not the number of rounds/interviewers considered)."""
        now = datetime.now(timezone.utc)
        queued_count = 0

        for schedule in self.interview_schedule_repo.get_ended_active_rounds(before=now):
            interviewers = self.interview_schedule_repo.get_active_interviewers(schedule.id)
            if not interviewers:
                continue

            campaign_candidate = self.campaign_candidate_repo.get_by_id(schedule.campaign_candidate_id)
            if campaign_candidate is None:
                continue

            queued_count += queue_pending_feedback_requests_for_round(
                self.db, campaign_candidate, schedule, interviewers, self.interview_feedback_repo,
            )

        return queued_count
