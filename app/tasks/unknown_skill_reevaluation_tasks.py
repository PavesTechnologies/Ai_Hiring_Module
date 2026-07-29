import logging
from uuid import UUID, uuid4

from app.core.celery_app import celery_app
from app.db.session import SessionLocal
from app.repositories.CampaignRepository import CampaignRepository
from app.repositories.campaign_candidate_repository import CampaignCandidateRepository
from app.repositories.candidate_repository import CandidateRepository
from app.repositories.celery_task_log_repository import CeleryTaskLogRepository
from app.services.campaign.candidate_reevaluation_validator import CandidateReEvaluationValidator
from app.services.celery_task_log_service import CeleryTaskLogService
from app.tasks.deterministic_scoring_tasks import (
    DETERMINISTIC_SCORE_TASK_TYPE,
    calculate_deterministic_score_task,
)

logger = logging.getLogger(__name__)

_REEVAL_IDEMPOTENCY_NAMESPACE = "UNKNOWN_SKILL_REEVAL"


@celery_app.task(name="scoring.reevaluate_candidates_for_unknown_skill", bind=True)
def reevaluate_candidates_for_unknown_skill_task(self, resume_ids: list[str], unknown_skill_id: str) -> None:
    """
    Fired after SkillCurationService resolves an UnknownSkill onto a
    canonical skill (map/create-canonical/bulk-approve) and one or more
    candidate_skills rows changed as a result. Never recomputes a score
    itself and never modifies candidate_scoring_service's or
    deterministic_scoring_tasks' scoring logic - for each affected
    campaign_candidate it only decides, via CandidateReEvaluationValidator,
    whether re-running the EXISTING calculate_deterministic_score_task is
    safe right now, and if so re-enqueues that exact task unchanged.

    Idempotent: re-running this task for the same (campaign_candidate_id,
    unknown_skill_id) pair - e.g. a Celery redelivery, or the same unknown
    skill affecting the same candidate through two different resumes -
    never double-enqueues scoring. Uses the same CeleryTaskLog
    idempotency_key pattern resume_processing_tasks._enqueue_deterministic_
    scoring already established, just under a distinct namespace so a
    re-evaluation trigger is never mistaken for the resume pipeline's own
    initial-scoring enqueue of the same campaign_candidate.
    """
    db = SessionLocal()
    try:
        campaign_candidate_repo = CampaignCandidateRepository(db)
        candidate_repo = CandidateRepository(db)
        campaign_repo = CampaignRepository(db)
        task_log_repo = CeleryTaskLogRepository(db)
        task_log_service = CeleryTaskLogService(task_log_repo)
        validator = CandidateReEvaluationValidator()

        seen_campaign_candidate_ids: set[UUID] = set()

        for resume_id in resume_ids:
            for campaign_candidate in campaign_candidate_repo.get_by_resume_id(UUID(resume_id)):
                if campaign_candidate.id in seen_campaign_candidate_ids:
                    continue
                seen_campaign_candidate_ids.add(campaign_candidate.id)

                candidate = candidate_repo.get_by_id(campaign_candidate.candidate_id)
                campaign = campaign_repo.get_by_id(campaign_candidate.campaign_id)

                decision = validator.evaluate(campaign_candidate, candidate, campaign)
                if not decision.allowed:
                    logger.info(
                        "Skipping unknown-skill candidate re-evaluation | campaign_candidate_id=%s "
                        "unknown_skill_id=%s reason=%s",
                        campaign_candidate.id, unknown_skill_id, decision.skip_reason,
                    )
                    continue

                idempotency_key = (
                    f"{_REEVAL_IDEMPOTENCY_NAMESPACE}:{campaign_candidate.id}:{unknown_skill_id}"
                )
                if task_log_repo.get_by_idempotency_key(idempotency_key) is not None:
                    logger.info(
                        "Unknown-skill candidate re-evaluation already queued/run | "
                        "campaign_candidate_id=%s unknown_skill_id=%s - skipping.",
                        campaign_candidate.id, unknown_skill_id,
                    )
                    continue

                scoring_task_id = str(uuid4())
                task_log_service.create_log(
                    task_id=scoring_task_id,
                    task_type=DETERMINISTIC_SCORE_TASK_TYPE,
                    idempotency_key=idempotency_key,
                    campaign_candidate_id=campaign_candidate.id,
                )
                try:
                    calculate_deterministic_score_task.apply_async(
                        kwargs={"campaign_candidate_id": str(campaign_candidate.id)},
                        task_id=scoring_task_id,
                    )
                except Exception:
                    logger.exception(
                        "Failed to enqueue deterministic re-scoring for campaign_candidate_id=%s "
                        "unknown_skill_id=%s", campaign_candidate.id, unknown_skill_id,
                    )
    finally:
        db.close()
