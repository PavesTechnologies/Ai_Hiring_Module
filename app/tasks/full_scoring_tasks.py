"""
Completing a no-LLM fallback (retire v1, phase B).

A candidate scored without the LLM (the daily cap, or a provider failure on the last attempt) who
was held for review is marked awaiting_full_scoring. Once quota is back, the hourly scan queues the
full scoring for each. The full outcome is applied only if no person has acted on the candidate
since the fallback; otherwise it is recorded (not acted on) and the stage is left alone.

Applied:
- REJECT: the candidate is rejected (from SCREENING or the review HOLD);
- PASS: the review cap is lifted - a candidate the AI step had capped at HOLD gets the AI's own
  recommendation (SHORTLIST moves it on); one still in SCREENING carries on uncapped;
- REVIEW: the candidate stays held for review, with the full scoring's reasons.
"""
import json
import logging
from datetime import datetime, timezone
from uuid import UUID, uuid4

from sqlalchemy import or_, select

from app.core.celery_app import celery_app
from app.db.session import SessionLocal
from app.enums.constants import ActionType, EntityType
from app.models.async_tasks import TaskStatus
from app.models.outcome_overrides import CandidateOutcomeOverride
from app.models.pipeline import (
    CampaignCandidateStageHistory, DecisionSource, DecisionType, PipelineStage, TransitionSource,
)
from app.repositories.allowed_transition_repository import AllowedTransitionRepository
from app.repositories.audit_repository import AuditRepository
from app.repositories.campaign_candidate_repository import CampaignCandidateRepository
from app.repositories.CampaignRepository import CampaignRepository
from app.repositories.celery_task_log_repository import CeleryTaskLogRepository
from app.repositories.config_repository import ConfigRepository
from app.repositories.interview_schedule_repository import InterviewScheduleRepository
from app.repositories.jd_repository import JDRepository
from app.repositories.resume_repository import ResumeRepository
from app.repositories.scoring_result_repository import ScoringResultRepository
from app.repositories.skill_repository import SkillRepository
from app.services.audit_service import AuditService
from app.services.campaign.scoring_v2 import (
    OUTCOME_NOT_RUN, OUTCOME_PASS, OUTCOME_PENDING, OUTCOME_REJECT, SCORING_VERSION,
)
from app.services.campaign.scoring_v2_service import FALLBACK_NEVER, ScoringV2Service, v2_rejection_reason
from app.services.campaign.stage_transition_service import StageTransitionService
from app.services.celery_task_log_service import CeleryTaskLogService

logger = logging.getLogger(__name__)

FULL_SCORING_SCAN_TASK_TYPE = "FULL_SCORING_SCAN"
FULL_SCORING_TASK_TYPE = "FULL_SCORING"
SCAN_BATCH = 50
# Calls one candidate's full scoring may need (3 votes per asked core skill, a paraphrase round):
# the scan stops queueing when this no longer fits under the daily cap.
CALLS_PER_CANDIDATE = 15
_IN_FLIGHT = (TaskStatus.QUEUED, TaskStatus.RUNNING, TaskStatus.RETRY)


def _v2_service(db):
    from app.services.llm.factory import resolve_active_provider
    from app.services.llm.model_selection import LLMPurpose
    return ScoringV2Service(db, SkillRepository(db), ConfigRepository(db),
                            provider_resolver=lambda: resolve_active_provider(db, LLMPurpose.EXTRACTION))


def human_acted_since(db, campaign_candidate_id: UUID, since: datetime) -> str | None:
    """What a person did to this candidate after `since` (a manual stage move or an override), or None."""
    move = db.execute(
        select(CampaignCandidateStageHistory)
        .where(CampaignCandidateStageHistory.campaign_candidate_id == campaign_candidate_id,
               CampaignCandidateStageHistory.changed_at > since,
               or_(CampaignCandidateStageHistory.transition_source != TransitionSource.SYSTEM,
                   CampaignCandidateStageHistory.changed_by.is_not(None)))
        .order_by(CampaignCandidateStageHistory.changed_at).limit(1)
    ).scalars().first()
    if move is not None:
        return f"moved to {move.to_stage.value} by a person at {move.changed_at.isoformat()}"
    override = db.execute(
        select(CandidateOutcomeOverride)
        .where(CandidateOutcomeOverride.campaign_candidate_id == campaign_candidate_id,
               CandidateOutcomeOverride.created_at > since)
        .order_by(CandidateOutcomeOverride.created_at).limit(1)
    ).scalars().first()
    if override is not None:
        return f"overridden ({override.reason_code}) at {override.created_at.isoformat()}"
    return None


TRANSITION_TRIGGER = "FULL_SCORING_AFTER_NO_LLM_FALLBACK"


def _audit_transition(audit_service, campaign_candidate, from_stage: PipelineStage) -> None:
    """The system's own stage move, audited like any transition (actor SYSTEM)."""
    if audit_service is None:
        return
    audit_service.log(
        actor_id=None, actor_role="SYSTEM", action_type=ActionType.PIPELINE_STAGE_TRANSITIONED,
        entity_type=EntityType.CAMPAIGN_CANDIDATE, entity_id=campaign_candidate.id,
        campaign_id=getattr(campaign_candidate, "campaign_id", None),
        details={"from_stage": from_stage.value, "to_stage": campaign_candidate.pipeline_stage.value,
                 "reason_provided": True, "trigger": TRANSITION_TRIGGER},
    )


def apply_full_outcome(stage_service: StageTransitionService, campaign_candidate, full: dict,
                       audit_service=None) -> str:
    """
    The stage effect of the full outcome on a candidate nobody acted on; returns what happened.
    A move made here is audited as PIPELINE_STAGE_TRANSITIONED with actor SYSTEM.
    """
    stage = campaign_candidate.pipeline_stage
    outcome = full["outcome"]
    if outcome == OUTCOME_REJECT:
        if stage not in (PipelineStage.SCREENING, PipelineStage.HOLD):
            return f"not rejected: candidate is in {stage.value}"
        moved = stage_service.transition_to_rejected(
            campaign_candidate, change_reason="Full scoring after the no-LLM fallback",
            scores_snapshot={"scoring": full.get("scoring_config_version"), "outcome": outcome},
            decision_source=DecisionSource.DETERMINISTIC, decision_reason=v2_rejection_reason(full),
            decision_details={"full_scoring": True, "failed_checks": full.get("failed_checks") or []},
        )
        if moved:
            _audit_transition(audit_service, campaign_candidate, stage)
        return "rejected" if moved else f"rejection not allowed from {stage.value}"
    if outcome == OUTCOME_PASS and stage == PipelineStage.HOLD:
        details = campaign_candidate.decision_details or {}
        recommendation = details.get("ai_recommendation") if details.get("capped_at_hold") else None
        if recommendation == "SHORTLIST":
            moved = stage_service.transition_on_ai_success(
                campaign_candidate, to_stage=PipelineStage.SHORTLISTED, decision_type=DecisionType.SHORTLISTED,
                change_reason="Full scoring after the no-LLM fallback: review cap lifted, AI recommendation applied",
                scores_snapshot=details.get("ai_response") or {},
                decision_reason="AI evaluation recommended shortlisting this candidate.",
                decision_details=details.get("ai_response"),
            )
            if moved:
                _audit_transition(audit_service, campaign_candidate, stage)
            return "shortlisted (AI recommendation, review cap lifted)" if moved else "shortlist not allowed"
        return "stays on HOLD (no capped AI shortlist to apply)"
    return "no stage change"


@celery_app.task(name="scoring.complete_full_scoring", bind=True)
def complete_full_scoring_task(self, campaign_candidate_id: str, fallback_result_id: str) -> None:
    from app.tasks.deterministic_scoring_tasks import build_validation_inputs, store_v2_decision

    db = SessionLocal()
    task_log_service = CeleryTaskLogService(CeleryTaskLogRepository(db))
    task_log = None
    try:
        existing = task_log_service.repository.get_by_task_id(self.request.id)
        if existing is not None and existing.status == TaskStatus.SUCCESS:
            return
        task_log = task_log_service.mark_running(existing or task_log_service.create_log(
            task_id=self.request.id, task_type=FULL_SCORING_TASK_TYPE,
            campaign_candidate_id=UUID(campaign_candidate_id)))

        results = ScoringResultRepository(db)
        fallback = results.get(UUID(fallback_result_id))
        candidate_repo = CampaignCandidateRepository(db)
        campaign_candidate = candidate_repo.get_by_id(UUID(campaign_candidate_id))
        if fallback is None or not fallback.awaiting_full_scoring or campaign_candidate is None:
            task_log_service.mark_success(task_log, summary=json.dumps({"skipped": "nothing awaiting"}))
            return
        campaign = CampaignRepository(db).get_by_id(campaign_candidate.campaign_id)
        resume = ResumeRepository(db).get_by_id(campaign_candidate.resume_id)
        job_description = JDRepository(db).get_by_id(campaign.jd_id)
        config_repo = ConfigRepository(db)
        experience, education, _, domain = build_validation_inputs(config_repo, job_description, resume)

        full = _v2_service(db).score(
            campaign=campaign, campaign_candidate=campaign_candidate, job_description=job_description,
            resume=resume, experience_result=experience, education_result=education, domain_result=domain,
            acted_on=False, llm_fallback=FALLBACK_NEVER,
        )
        if full["outcome"] in (OUTCOME_PENDING, OUTCOME_NOT_RUN):
            db.commit()                                   # the attempt stays on record; still awaiting
            task_log_service.mark_success(task_log, summary=json.dumps({"still_unavailable": full["outcome"]}))
            return

        full_row = results.latest(campaign_candidate.id, SCORING_VERSION)
        fallback.awaiting_full_scoring = False
        acted_by_person = human_acted_since(db, campaign_candidate.id, fallback.created_at)
        completion = {"completes_fallback_result_id": str(fallback.id), "applied": acted_by_person is None}
        if acted_by_person:
            completion["not_applied_reason"] = f"a person acted on the candidate since the fallback: {acted_by_person}"
            effect = "recorded only"
            # Nothing else on the candidate changes, but the wait is over (the badge goes).
            campaign_candidate.deterministic_breakdown = {**(campaign_candidate.deterministic_breakdown or {}),
                                                          "awaiting_full_scoring": False,
                                                          "full_scoring_recorded_only": True}
            candidate_repo.update(campaign_candidate)
        else:
            full_row.acted_on, fallback.acted_on = True, False
            store_v2_decision(campaign_candidate, {**full, "awaiting_full_scoring": False})
            stage_service = StageTransitionService(
                AllowedTransitionRepository(db), candidate_repo, AuditService(AuditRepository(db)),
                InterviewScheduleRepository(db))
            effect = apply_full_outcome(stage_service, campaign_candidate, full, AuditService(AuditRepository(db)))
            candidate_repo.update(campaign_candidate)
        completion["effect"] = effect
        full_row.breakdown = {**(full_row.breakdown or {}), **completion}
        AuditService(AuditRepository(db)).log(
            actor_id=None, actor_role="SYSTEM", action_type=ActionType.CANDIDATE_RESCORE_REQUESTED,
            entity_type=EntityType.CAMPAIGN_CANDIDATE, entity_id=campaign_candidate.id, campaign_id=campaign.id,
            details={"trigger": "FULL_SCORING_AFTER_NO_LLM_FALLBACK", "outcome": full["outcome"],
                     "applied": completion["applied"], "effect": effect},
        )
        db.commit()
        task_log_service.mark_success(task_log, summary=json.dumps(
            {"outcome": full["outcome"], "applied": completion["applied"], "effect": effect}))
    except Exception as ex:
        db.rollback()
        if task_log:
            task_log_service.mark_failure(task_log, str(ex))
        logger.exception("Full scoring after the fallback failed | campaign_candidate_id=%s", campaign_candidate_id)
    finally:
        db.close()


@celery_app.task(name="scoring.complete_full_scoring_scan")
def complete_full_scoring_scan() -> None:
    """Hourly: queue the full scoring for fallback HOLDs while the daily cap leaves room for them."""
    db = SessionLocal()
    task_log_service = CeleryTaskLogService(CeleryTaskLogRepository(db))
    task_log = None
    try:
        task_log = task_log_service.mark_running(
            task_log_service.create_log(task_id=str(uuid4()), task_type=FULL_SCORING_SCAN_TASK_TYPE))
        waiting = ScoringResultRepository(db).awaiting_full_scoring(limit=SCAN_BATCH)
        service = _v2_service(db) if waiting else None
        queued = in_flight = 0
        quota_reached = False
        for row in waiting:
            logs = task_log_service.repository.get_by_campaign_candidate_and_task_type(
                row.campaign_candidate_id, FULL_SCORING_TASK_TYPE)
            if any(log.status in _IN_FLIGHT for log in logs):
                in_flight += 1
                continue
            if not service._budget(CALLS_PER_CANDIDATE * (queued + 1)):
                quota_reached = True
                break
            task_id = str(uuid4())
            task_log_service.create_log(task_id=task_id, task_type=FULL_SCORING_TASK_TYPE,
                                        campaign_candidate_id=row.campaign_candidate_id)
            complete_full_scoring_task.apply_async(
                kwargs={"campaign_candidate_id": str(row.campaign_candidate_id), "fallback_result_id": str(row.id)},
                task_id=task_id)
            queued += 1
        summary = {"waiting": len(waiting), "queued": queued, "in_flight": in_flight, "quota_reached": quota_reached,
                   "at": datetime.now(timezone.utc).isoformat()}
        task_log_service.mark_success(task_log, summary=json.dumps(summary))
        logger.info("Full scoring scan | %s", summary)
    except Exception as ex:
        db.rollback()
        if task_log:
            task_log_service.mark_failure(task_log, str(ex))
        logger.exception("Full scoring scan failed")
    finally:
        db.close()
