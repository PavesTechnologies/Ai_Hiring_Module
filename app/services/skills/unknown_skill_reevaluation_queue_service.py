import logging
from uuid import UUID

from app.tasks.unknown_skill_reevaluation_tasks import reevaluate_candidates_for_unknown_skill_task

logger = logging.getLogger(__name__)


class ReEvaluationQueueError(Exception):
    """Raised when the candidate re-evaluation job could not be queued (e.g. the Celery broker is unreachable)."""

    def __init__(self, message: str, *, unknown_skill_id: UUID):
        super().__init__(message)
        self.unknown_skill_id = unknown_skill_id


class UnknownSkillReEvaluationQueueService:
    """
    Single entry point for queuing reevaluate_candidates_for_unknown_skill_task
    after an HR unknown-skill resolution migrates one or more candidate_skills
    rows onto a canonical skill. Mirrors EmbeddingQueueService's shape
    (structured logging, raise on failure so the caller can log/continue)
    rather than duplicating it.
    """

    def queue_reevaluation(self, unknown_skill_id: UUID, affected_resume_ids: set[UUID]) -> None:
        logger.info(
            "Queuing candidate re-evaluation | unknown_skill_id=%s resume_count=%d",
            unknown_skill_id, len(affected_resume_ids),
        )
        try:
            reevaluate_candidates_for_unknown_skill_task.apply_async(
                kwargs={
                    "resume_ids": [str(resume_id) for resume_id in affected_resume_ids],
                    "unknown_skill_id": str(unknown_skill_id),
                },
            )
        except Exception as exc:
            logger.exception(
                "Failed to queue candidate re-evaluation | unknown_skill_id=%s", unknown_skill_id,
            )
            raise ReEvaluationQueueError(
                f"Failed to queue candidate re-evaluation for unknown_skill '{unknown_skill_id}': {exc}",
                unknown_skill_id=unknown_skill_id,
            ) from exc
