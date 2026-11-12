"""
Shadow scoring task (retire v1, phase D): scores one campaign candidate with the
campaign's candidate scoring version (hiring_campaigns.candidate_scoring_version_id),
AFTER the current version has decided and committed. Stored with acted_on = false;
never touches campaign_candidates - the decision is unaffected by anything that
happens here, including failures.

PENDING (the core confirmation couldn't run: quota, provider down, backup
failing too) is retried with backoff; when retries run out the task is
dead-lettered (task type SCORING_V2) and can be replayed from the existing
failure queue. A provider failure never turns into a REJECT.
"""
import json
import logging
from datetime import datetime, timezone
from uuid import UUID, uuid4

from app.core.safe_errors import safe_error
from app.core.celery_app import bounded_countdown, celery_app
from app.core.task_dispatch import dispatch_or_mark_lost, is_in_flight
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
from app.services.campaign.scoring_v2_service import ScoringV2Service
from app.services.campaign.scoring_versions import STATUS_CANDIDATE
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
        is_in_flight(log)       # review item 8: a stale row no longer blocks a re-queue
        for log in task_log_repo.get_by_campaign_candidate_and_task_type(campaign_candidate.id, SCORING_V2_TASK_TYPE)
    ):
        return False
    log = task_log_service.create_log(
        task_id=str(uuid4()), task_type=SCORING_V2_TASK_TYPE, campaign_candidate_id=campaign_candidate.id,
    )
    return dispatch_or_mark_lost(calculate_scoring_v2_task, kwargs={"campaign_candidate_id": str(campaign_candidate.id)},
                                 log=log, task_log_service=task_log_service)


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
        from app.models.scoring_v2 import ScoringVersion
        version_id = getattr(campaign, "candidate_scoring_version_id", None) if campaign is not None else None
        version = db.get(ScoringVersion, version_id) if version_id is not None else None
        if campaign_candidate is None or version is None or version.status != STATUS_CANDIDATE:
            task_log_service.mark_success(task_log, summary=json.dumps(
                {"skipped": True, "reason": "the campaign is not trying a candidate scoring version"}))
            return
        resume = ResumeRepository(db).get_by_id(campaign_candidate.resume_id)
        job_description = JDRepository(db).get_by_id(campaign.jd_id)

        # Local import - deterministic_scoring_tasks imports this module.
        from app.services.campaign.validation_inputs import build_validation_inputs
        from app.services.llm.factory import resolve_active_provider
        from app.services.llm.model_selection import LLMPurpose

        config_repo = ConfigRepository(db)
        # The candidate version's own validation settings (settings phase 3), not the current version's.
        from app.services.campaign.scoring_versions import version_config
        experience, education, domain = build_validation_inputs(config_repo, job_description, resume,
                                                                config=version_config(version))
        breakdown = ScoringV2Service(
            db, SkillRepository(db), config_repo,
            provider_resolver=lambda: resolve_active_provider(db, LLMPurpose.EXTRACTION),
        ).score(
            campaign=campaign, campaign_candidate=campaign_candidate, job_description=job_description,
            resume=resume, experience_result=experience, education_result=education, domain_result=domain,
            acted_on=False, scoring_version=version,
        )
        db.commit()  # the stored row (PENDING too) - the shadow report shows the latest

        if breakdown["outcome"] == OUTCOME_PENDING:
            raise ScoringV2Pending(breakdown.get("outcome_reason") or "core confirmation unavailable")
        task_log_service.mark_success(task_log, summary=json.dumps({
            "scoring_version": version.label,
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
            self.retry(exc=ex, countdown=bounded_countdown(delay), max_retries=_SCORING_V2_RETRY_POLICY.max_attempts)
            return
        try:
            DeadLetterQueueRepository(db).create(
                original_task_id=task_id, task_type=SCORING_V2_TASK_TYPE, final_error_message=safe_error(ex),
                input_payload={"campaign_candidate_id": campaign_candidate_id}, retry_count=attempt_number,
                first_attempted_at=task_log.queued_at if task_log else datetime.now(timezone.utc),
                last_attempted_at=datetime.now(timezone.utc), campaign_candidate_id=UUID(campaign_candidate_id),
            )
            DeadLetterQueueRepository(db).commit()
        except Exception:
            logger.exception("Failed to dead-letter SCORING_V2 for campaign_candidate_id=%s", campaign_candidate_id)
            db.rollback()
        if task_log:
            task_log_service.mark_dead(task_log, safe_error(ex))
        logger.error("Scoring v2 gave up (dead-lettered) | campaign_candidate_id=%s: %s", campaign_candidate_id, ex)
    finally:
        db.close()
