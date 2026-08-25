from app.core.celery_app import celery_app
from app.db.session import SessionLocal
from app.repositories.campaign_candidate_repository import CampaignCandidateRepository
from app.repositories.celery_task_log_repository import CeleryTaskLogRepository
from app.repositories.interview_feedback_repository import InterviewFeedbackRepository
from app.repositories.interview_schedule_repository import InterviewScheduleRepository
from app.services.celery_task_log_service import CeleryTaskLogService
from app.services.interview_feedback_request_sweep_service import InterviewFeedbackRequestSweepService


@celery_app.task(name="interview.request_feedback_for_ended_rounds", bind=True)
def request_interview_feedback_task(self):
    """
    Epic 5 Step 4 - hourly sweep queueing INTERVIEW_FEEDBACK_REQUESTED
    emails for every (round, interviewer) pair whose round has ended
    (SCHEDULED/RESCHEDULED, end_at < now()) and hasn't yet given feedback
    or already been emailed. See InterviewFeedbackRequestSweepService for
    the actual logic - this task is a thin wrapper, matching every other
    periodic job in this codebase (e.g. campaign.detect_stalled_candidates).
    """
    db = SessionLocal()
    task_log = None
    try:
        task_log_repo = CeleryTaskLogRepository(db)
        task_log_service = CeleryTaskLogService(task_log_repo)
        task_log = task_log_service.create_log(
            task_id=self.request.id, task_type="INTERVIEW_FEEDBACK_REQUEST_SWEEP",
        )

        sweep_service = InterviewFeedbackRequestSweepService(
            db,
            InterviewScheduleRepository(db),
            InterviewFeedbackRepository(db),
            CampaignCandidateRepository(db),
        )
        queued_count = sweep_service.run()

        task_log_service.mark_success(
            task_log,
            summary=(
                "No feedback-request emails queued." if queued_count == 0
                else f"Queued {queued_count} feedback-request email(s)."
            ),
        )
        return queued_count

    except Exception as ex:
        if task_log:
            task_log_service.mark_failure(task_log, str(ex))
        raise

    finally:
        db.close()
