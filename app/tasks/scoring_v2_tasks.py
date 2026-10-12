"""
Scoring v2 shadow task (Phase 1): computes and stores v2 for one campaign
candidate on a scoring_v2_shadow / scoring_v2_enabled campaign, AFTER v1 has
decided and committed. Never touches campaign_candidates - v1 is unaffected
by anything that happens here, including failures.

PENDING (the core confirmation couldn't run: quota, provider down, backup
failing too) is retried with backoff; when retries run out the task is
dead-lettered (task type SCORING_V2) and can be replayed from the existing
failure queue. A provider failure never turns into a REJECT.
"""
import json
import logging
from datetime import datetime, timezone
from uuid import UUID, uuid4

from app.core.celery_app import celery_app
from app.db.session import SessionLocal
from app.models.async_tasks import TaskStatus
from app.repositories.CampaignRepository import CampaignRepository
from app.repositories.campaign_candidate_repository import CampaignCandidateRepository
from app.repositories.celery_task_log_repository import CeleryTaskLogRepository
from app.repositories.config_repository import ConfigRepository
from app.repositories.dead_letter_queue_repository import DeadLetterQueueRepository
from app.repositories.jd_repository import JDRepository
from app.repositories.resume_repository import ResumeRepository
from app.repositories.skill_repository import SkillRepository
from app.services.campaign.scoring_v2 import OUTCOME_PENDING
from app.services.campaign.scoring_v2_service import ScoringV2Service, v2_should_run
from app.services.celery_task_log_service import CeleryTaskLogService
from app.services.document_processing.retry_policy import RetryPolicy, compute_backoff_seconds

logger = logging.getLogger(__name__)

SCORING_V2_TASK_TYPE = "SCORING_V2"

# Quota / outages clear in minutes, not seconds - longer than AI_EVALUATE's policy.
_SCORING_V2_RETRY_POLICY = RetryPolicy(max_attempts=4, base_delay_seconds=120, max_delay_seconds=1800)


class ScoringV2Pending(Exception):
    """The core confirmation couldn't run - retry later."""


def enqueue_scoring_v2(campaign_candidate, task_log_service: CeleryTaskLogService) -> bool:
    """Queues the shadow v2 task unless one is already queued/running for this candidate."""
    task_log_repo = task_log_service.repository
    if any(
        log.status in (TaskStatus.QUEUED, TaskStatus.RUNNING, TaskStatus.RETRY)
        for log in task_log_repo.get_by_campaign_candidate_and_task_type(campaign_candidate.id, SCORING_V2_TASK_TYPE)
    ):
        return False
    log = task_log_service.create_log(
        task_id=str(uuid4()), task_type=SCORING_V2_TASK_TYPE, campaign_candidate_id=campaign_candidate.id,
    )
    try:
        calculate_scoring_v2_task.apply_async(
            kwargs={"campaign_candidate_id": str(campaign_candidate.id)}, task_id=log.task_id,
        )
        return True
    except Exception:
        logger.exception("Failed to enqueue SCORING_V2 for campaign_candidate_id=%s", campaign_candidate.id)
        return False


@celery_app.task(name="scoring.calculate_scoring_v2", bind=True)
def calculate_scoring_v2_task(self, campaign_candidate_id: str) -> None:
    db = SessionLocal()
    task_id = self.request.id
    attempt_number = self.request.retries + 1
    task_log = None
    task_log_service = CeleryTaskLogService(CeleryTaskLogRepository(db))
    try:
        existing = task_log_service.repository.get_by_task_id(task_id)
        if existing is not None and existing.status == TaskStatus.SUCCESS:
            return
        if existing is None:
            existing = task_log_service.create_log(
                task_id=task_id, task_type=SCORING_V2_TASK_TYPE, campaign_candidate_id=UUID(campaign_candidate_id),
            )
        task_log = task_log_service.mark_running(existing)

        campaign_candidate = CampaignCandidateRepository(db).get_by_id(UUID(campaign_candidate_id))
        campaign = CampaignRepository(db).get_by_id(campaign_candidate.campaign_id) if campaign_candidate else None
        if campaign_candidate is None or campaign is None or not v2_should_run(campaign):
            task_log_service.mark_success(task_log, summary=json.dumps({"skipped": True, "reason": "v2 not enabled"}))
            return
        resume = ResumeRepository(db).get_by_id(campaign_candidate.resume_id)
        job_description = JDRepository(db).get_by_id(campaign.jd_id)

        # Local import - deterministic_scoring_tasks imports this module.
        from app.tasks.deterministic_scoring_tasks import build_validation_inputs
        from app.services.llm.factory import resolve_active_provider
        from app.services.llm.model_selection import LLMPurpose

        config_repo = ConfigRepository(db)
        experience, education, _, domain = build_validation_inputs(config_repo, job_description, resume)
        breakdown = ScoringV2Service(
            db, SkillRepository(db), config_repo,
            provider_resolver=lambda: resolve_active_provider(db, LLMPurpose.EXTRACTION),
        ).score(
            campaign=campaign, campaign_candidate=campaign_candidate, job_description=job_description,
            resume=resume, experience_result=experience, education_result=education, domain_result=domain,
            acted_on=False,
        )
        db.commit()  # the stored v2 row (PENDING too) - the shadow report shows the latest

        if breakdown["outcome"] == OUTCOME_PENDING:
            raise ScoringV2Pending(breakdown.get("outcome_reason") or "core confirmation unavailable")
        task_log_service.mark_success(task_log, summary=json.dumps({
            "outcome": breakdown["outcome"], "score": breakdown.get("deterministic_score"),
            "llm_calls": (breakdown.get("confirmation") or {}).get("llm_calls"),
        }))

    except Exception as ex:
        db.rollback()
        if attempt_number < _SCORING_V2_RETRY_POLICY.max_attempts:
            if task_log:
                task_log_service.mark_retry(task_log)
            delay = compute_backoff_seconds(_SCORING_V2_RETRY_POLICY, attempt_number)
            logger.warning("Scoring v2 pending/failed, retrying | campaign_candidate_id=%s attempt=%s delay=%ss: %s",
                           campaign_candidate_id, attempt_number, delay, ex)
            self.retry(exc=ex, countdown=delay, max_retries=_SCORING_V2_RETRY_POLICY.max_attempts)
            return
        try:
            DeadLetterQueueRepository(db).create(
                original_task_id=task_id, task_type=SCORING_V2_TASK_TYPE, final_error_message=str(ex)[:2000],
                input_payload={"campaign_candidate_id": campaign_candidate_id}, retry_count=attempt_number,
                first_attempted_at=task_log.queued_at if task_log else datetime.now(timezone.utc),
                last_attempted_at=datetime.now(timezone.utc), campaign_candidate_id=UUID(campaign_candidate_id),
            )
            DeadLetterQueueRepository(db).commit()
        except Exception:
            logger.exception("Failed to dead-letter SCORING_V2 for campaign_candidate_id=%s", campaign_candidate_id)
            db.rollback()
        if task_log:
            task_log_service.mark_dead(task_log, str(ex))
        logger.error("Scoring v2 gave up (dead-lettered) | campaign_candidate_id=%s: %s", campaign_candidate_id, ex)
    finally:
        db.close()
