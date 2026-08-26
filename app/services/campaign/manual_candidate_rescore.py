import logging

from app.models.async_tasks import TaskStatus
from app.models.candidates import ParseStatus
from app.repositories.CampaignRepository import CampaignRepository
from app.repositories.campaign_candidate_ai_evaluation_repository import CampaignCandidateAIEvaluationRepository
from app.repositories.campaign_candidate_repository import CampaignCandidateRepository
from app.repositories.candidate_repository import CandidateRepository
from app.repositories.celery_task_log_repository import CeleryTaskLogRepository
from app.repositories.resume_repository import ResumeRepository
from app.services.campaign.candidate_reevaluation_validator import CandidateReEvaluationValidator

logger = logging.getLogger(__name__)

# Duplicated from semantic_scoring_tasks.py/ai_evaluation_tasks.py rather
# than imported - same reasoning as AI_EVALUATE_TASK_TYPE's own duplication
# there (see that file's comment): importing it here would risk the same
# circular-import shape this module already works around for
# calculate_deterministic_score_task.
SEMANTIC_SCORE_TASK_TYPE = "SEMANTIC_SCORE"
AI_EVALUATE_TASK_TYPE = "AI_EVALUATE"

"""
Epic 5 follow-up - manual re-score trigger. Fired when a candidate is
manually moved TO SCREENING from any stage other than UPLOADED (the
UPLOADED case is the automated resume-upload path, which already scores
the candidate itself via StageTransitionService.transition_to_screening()
- this function must never be wired to that path, or the scoring task's
own entry into SCREENING would re-trigger another scoring task,
indefinitely).

Reuses CandidateReEvaluationValidator - originally built for the
unknown-skill re-evaluation trigger, but its checks (candidate not
erased, campaign ACTIVE/PAUSED, pipeline_stage eligible) are exactly
what any "is it safe to re-score this existing candidate right now"
caller needs, not something specific to that one trigger.

No idempotency-key/dedup mechanism here, unlike unknown_skill_
reevaluation_tasks.py's namespaced key - deliberately: every real caller
of this function (StageTransitionService.transition(),
PipelineTransitionService.transition_stage() and CampaignService.
override_candidate_stage()'s callers) already has its own guard against
a duplicate call for the same event before this function is ever
reached (transition()'s own idempotency_key short-circuits a retry
before its hooks run at all; move_one/bulk_move/override_candidate_stage
all reject a request where from_stage == to_stage, so "move to
SCREENING" can never be submitted twice in a row for the same candidate
without a genuinely different transition happening in between - which
is a legitimate new re-score event, not a duplicate).

Best-effort, post-commit, same reasoning as every other Celery-
enqueueing hook in this codebase (_queue_post_override_evaluation,
candidate_notification_emails.py): a failure to re-score must never
undo the already-committed stage move.
"""


def enqueue_manual_rescore(db, campaign_candidate) -> bool:
    """Returns True if a re-score was actually queued, False if skipped (and why, logged) or failed."""
    # Local import - deterministic_scoring_tasks.py imports StageTransitionService
    # (for transition_to_screening), and StageTransitionService imports this
    # module - a module-level import here would be circular. Same lazy-import
    # workaround already used elsewhere in this codebase for this exact shape
    # of cross-cutting dependency (e.g. campaign_candidate_service.py's
    # _degree_level_display import inside a method body).
    from app.tasks.deterministic_scoring_tasks import calculate_deterministic_score_task

    try:
        candidate = CandidateRepository(db).get_by_id(campaign_candidate.candidate_id)
        campaign = CampaignRepository(db).get_by_id(campaign_candidate.campaign_id)

        decision = CandidateReEvaluationValidator().evaluate(campaign_candidate, candidate, campaign)
        if not decision.allowed:
            logger.info(
                "Skipping manual re-score | campaign_candidate_id=%s reason=%s",
                campaign_candidate.id, decision.skip_reason,
            )
            return False

        resume = ResumeRepository(db).get_by_id(campaign_candidate.resume_id) if campaign_candidate.resume_id else None
        if resume is None or resume.parse_status != ParseStatus.PARSED:
            logger.info(
                "Skipping manual re-score - resume not parsed | campaign_candidate_id=%s", campaign_candidate.id,
            )
            return False

        campaign_candidate_repo = CampaignCandidateRepository(db)
        campaign_candidate_repo.reset_for_rescore(campaign_candidate)

        ai_evaluation_repo = CampaignCandidateAIEvaluationRepository(db)
        ai_evaluation_repo.reset(ai_evaluation_repo.get_or_create(campaign_candidate.id))

        # Found live: a candidate's prior AI_EVALUATE celery_task_log row can
        # get orphaned at QUEUED forever (e.g. the candidate moved off
        # SCREENING before a worker ever picked it up) - _enqueue_ai_evaluation
        # treats any existing QUEUED/RUNNING row as "already in flight" and
        # silently skips dispatching a fresh one, even 10 days later.
        # SEMANTIC_SCORE has the identical exposure via its own idempotency
        # key (semantic_scoring_tasks.py's _semantic_score_idempotency_key is
        # a permanent hash of just the campaign_candidate_id, no per-attempt
        # component) - _enqueue_semantic_scoring treats an existing
        # QUEUED/RUNNING/SUCCESS row as "already scored/in flight, nothing to
        # do". At this point nothing for THIS candidate's fresh attempt has
        # been dispatched yet (deterministic scoring - the only thing that
        # can ever (re-)enqueue either of these - hasn't been called below
        # yet), so any existing row for either task type is guaranteed to be
        # stale, regardless of status.
        celery_task_log_repo = CeleryTaskLogRepository(db)
        for log in celery_task_log_repo.get_by_campaign_candidate_and_task_type(
            campaign_candidate.id, SEMANTIC_SCORE_TASK_TYPE,
        ):
            if log.status in (TaskStatus.SUCCESS, TaskStatus.QUEUED, TaskStatus.RUNNING):
                celery_task_log_repo.delete_by_task_id(log.task_id)
        for log in celery_task_log_repo.get_by_campaign_candidate_and_task_type(
            campaign_candidate.id, AI_EVALUATE_TASK_TYPE,
        ):
            if log.status in (TaskStatus.QUEUED, TaskStatus.RUNNING):
                celery_task_log_repo.delete_by_task_id(log.task_id)

        campaign_candidate_repo.commit()

        calculate_deterministic_score_task.apply_async(kwargs={"campaign_candidate_id": str(campaign_candidate.id)})
        return True
    except Exception:
        logger.exception("Failed to enqueue manual re-score | campaign_candidate_id=%s", campaign_candidate.id)
        return False
