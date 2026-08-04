import json
import logging
from uuid import uuid4

from app.core.celery_app import celery_app
from app.db.session import SessionLocal
from app.models.pipeline import PipelineStage
from app.repositories.campaign_candidate_repository import CampaignCandidateRepository
from app.repositories.candidate_repository import CandidateRepository
from app.repositories.celery_task_log_repository import CeleryTaskLogRepository
from app.repositories.resume_repository import ResumeRepository
from app.services.celery_task_log_service import CeleryTaskLogService

logger = logging.getLogger(__name__)

TALENT_POOL_ELIGIBILITY_TASK_TYPE = "TALENT_POOL_ELIGIBILITY_VALIDATION"


def _compute_should_be_eligible(candidate, latest_campaign_candidate) -> bool:
    """
    Talent Pool Eligibility: FALSE only when one of the spec's named
    conditions holds - otherwise TRUE. erasure_requested_at is checked
    alongside is_pii_deleted (not just is_pii_deleted alone): the erasure
    "requested" phase (CandidateErasureService.request_erasure) zeroes out
    embeddings and sets ineligible immediately, before is_pii_deleted is
    necessarily set - without this check, this reconciliation task would
    otherwise flip a zeroed, request-pending embedding back to "eligible"
    the very next time it runs. is_pii_deleted is one-way (nothing in this
    codebase ever clears it back to False) - once True, always ineligible
    from here on. Withdrawn application is out of scope (future support,
    per spec) - always False today, so it never disqualifies anything yet.
    """
    if candidate.is_pii_deleted or candidate.erasure_requested_at is not None:
        return False

    if (
        latest_campaign_candidate is not None
        and latest_campaign_candidate.pipeline_stage == PipelineStage.FRAUD_REVIEW
        and latest_campaign_candidate.is_fraud_flagged
    ):
        return False

    return True


@celery_app.task(name="embedding.validate_talent_pool_eligibility")
def validate_talent_pool_eligibility() -> None:
    """
    Talent Pool Eligibility requirement: daily Celery Beat job that
    validates and corrects is_talent_pool_eligible on every
    resume_embeddings row, since eligibility can become stale in either
    direction after the embedding was created - a candidate can request
    erasure or get fraud-flagged later (should flip TRUE -> FALSE), or a
    fraud flag can later be cleared via resubmission (should flip
    FALSE -> TRUE, except once is_pii_deleted/erasure_requested_at is set,
    which is permanent).

    Only evaluates candidates that actually have at least one
    resume_embeddings row (get_distinct_candidate_ids_with_embeddings) -
    never scans every candidate in the system. Idempotent: re-running this
    with nothing having changed applies zero corrections.
    """
    db = SessionLocal()
    task_log = None
    try:
        resume_repo = ResumeRepository(db)
        candidate_repo = CandidateRepository(db)
        campaign_candidate_repo = CampaignCandidateRepository(db)
        task_log_repo = CeleryTaskLogRepository(db)
        task_log_service = CeleryTaskLogService(task_log_repo)

        task_log = task_log_service.create_log(
            task_id=str(uuid4()),
            task_type=TALENT_POOL_ELIGIBILITY_TASK_TYPE,
        )

        candidate_ids = resume_repo.get_distinct_candidate_ids_with_embeddings()
        candidates_evaluated = 0
        rows_corrected = 0

        for candidate_id in candidate_ids:
            candidate = candidate_repo.get_by_id(candidate_id)
            if candidate is None:
                # Orphaned embedding row (e.g. the candidate was hard-deleted
                # via CandidateErasureService.erase_candidate, which already
                # deletes resume_embeddings itself - this should be rare/
                # transient, never this task's concern to clean up).
                continue

            campaign_candidates = campaign_candidate_repo.get_by_candidate_id(candidate_id)
            latest_campaign_candidate = (
                max(campaign_candidates, key=lambda cc: cc.created_at) if campaign_candidates else None
            )
            should_be_eligible = _compute_should_be_eligible(candidate, latest_campaign_candidate)

            embeddings = resume_repo.get_embeddings_by_candidate(candidate_id)
            mismatched = [e for e in embeddings if e.is_talent_pool_eligible != should_be_eligible]

            candidates_evaluated += 1
            if mismatched:
                resume_repo.set_talent_pool_eligibility_for_candidate(candidate_id, should_be_eligible)
                rows_corrected += len(mismatched)

        db.commit()

        summary = json.dumps({
            "candidates_evaluated": candidates_evaluated,
            "rows_corrected": rows_corrected,
        })
        task_log_service.mark_success(task_log, summary=summary)
        logger.info(
            "Talent pool eligibility validation completed | candidates_evaluated=%s rows_corrected=%s",
            candidates_evaluated, rows_corrected,
        )

    except Exception as ex:
        db.rollback()
        if task_log:
            task_log_service.mark_failure(task_log, str(ex))
        logger.exception("Talent pool eligibility validation failed")

    finally:
        db.close()
