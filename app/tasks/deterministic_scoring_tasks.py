import json
import logging
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

from app.core.celery_app import celery_app
from app.db.session import SessionLocal
from app.enums.constants import ActionType, EntityType
from app.models.async_tasks import TaskStatus
from app.models.campaigns import CampaignStatus
from app.models.candidates import ParseStatus
from app.models.pipeline import AIEvaluationStatus, CompositeScoreTriggerSource, DecisionSource, PipelineStage
from app.repositories.allowed_transition_repository import AllowedTransitionRepository
from app.repositories.audit_repository import AuditRepository
from app.repositories.campaign_candidate_ai_evaluation_repository import CampaignCandidateAIEvaluationRepository
from app.repositories.campaign_candidate_repository import CampaignCandidateRepository
from app.repositories.CampaignRepository import CampaignRepository
from app.repositories.celery_task_log_repository import CeleryTaskLogRepository
from app.repositories.config_repository import ConfigRepository
from app.repositories.interview_schedule_repository import InterviewScheduleRepository
from app.repositories.jd_repository import JDRepository
from app.repositories.resume_repository import ResumeRepository
from app.repositories.skill_repository import SkillRepository
from app.services.audit_service import AuditService
from app.services.campaign.domain_capability_matching_service import DomainCapabilityMatchingService
from app.repositories.dead_letter_queue_repository import DeadLetterQueueRepository
from app.services.campaign.scoring_v2 import OUTCOME_NOT_RUN, OUTCOME_PENDING
from app.services.document_processing.retry_policy import RetryPolicy, compute_backoff_seconds
from app.services.campaign.scoring_v2_service import (
    FALLBACK_NEVER, FALLBACK_ON_CAP, FALLBACK_ON_UNAVAILABLE, ScoringV2Service, v2_rejection_reason,
)
from app.services.campaign.experience_education_validation_service import (
    ExperienceEducationValidationService,
)
from app.services.campaign.stage_transition_service import StageTransitionService
from app.services.celery_task_log_service import CeleryTaskLogService
from app.services.resume.work_experience_duration import experience_years_for_scoring
from app.tasks.composite_scoring_tasks import _enqueue_composite_scoring
from app.services.candidate_change_notifier import CandidateChangeNotifier
from app.services.education.education_field_family_service import field_families_for_jd
from app.services.jd.jd_calibration_rules import (
    apply_education_requirement, apply_experience_tolerance, experience_minimum_inputs, jd_internship_credit,
)

logger = logging.getLogger(__name__)

DETERMINISTIC_SCORE_TASK_TYPE = "DETERMINISTIC_SCORE"


_SCOREABLE_CAMPAIGN_STATUSES = {CampaignStatus.ACTIVE, CampaignStatus.PAUSED}

# v2 decides but couldn't confirm (PENDING: provider failure; NOT_RUN: daily LLM call cap): the
# whole scoring is retried with backoff (~30 min in all), then dead-lettered (replayable); the
# hourly recovery job re-queues candidates whose decision never landed. Never a REJECT.
_DECISION_PENDING_RETRY_POLICY = RetryPolicy(max_attempts=4, base_delay_seconds=120, max_delay_seconds=1800)
_UNDECIDED_OUTCOMES = (OUTCOME_PENDING, OUTCOME_NOT_RUN)
DECISION_RECOVERY_TASK_TYPE = "SCORING_DECISION_RECOVERY_SCAN"
_RECOVERY_HOURS_KEY = "SCORING_DECISION_RECOVERY_HOURS"
_DEFAULT_RECOVERY_HOURS = 6.0
_RECOVERY_GRACE = timedelta(minutes=30)
_IN_FLIGHT = (TaskStatus.QUEUED, TaskStatus.RUNNING, TaskStatus.RETRY)


_EXPERIENCE_TOLERANCE_YEARS_KEY = "EXPERIENCE_TOLERANCE_YEARS"
_EQUIVALENT_EXPERIENCE_YEARS_KEY = "EQUIVALENT_EXPERIENCE_YEARS"
_DETERMINISTIC_WEIGHT_SKILLS_KEY = "DETERMINISTIC_WEIGHT_SKILLS"
_DETERMINISTIC_WEIGHT_EXPERIENCE_KEY = "DETERMINISTIC_WEIGHT_EXPERIENCE"
_DETERMINISTIC_WEIGHT_EDUCATION_KEY = "DETERMINISTIC_WEIGHT_EDUCATION"
_DETERMINISTIC_WEIGHT_FUNCTIONAL_KEY = "DETERMINISTIC_WEIGHT_FUNCTIONAL"


def build_validation_inputs(config_repo, job_description, resume):
    """
    (experience_result, education_result, score_weights, domain_result) for a
    resume against a JD - the deterministic layer's non-skill inputs, shared
    by this task and the scoring v2 dry-run script.
    """
    parsed_json = resume.parsed_json or {}
    # Same "JSON is the single source of truth" preference as
    # education/JD-experience above: prefer total_experience_years
    # computed from work_experience's own start_date/end_date (the same
    # computation the resume-parsed-json display endpoint already
    # applies, per annotate_work_experience_durations' own docstring -
    # it doesn't trust the AI-extracted figure verbatim since that can
    # drift from what the listed dates actually add up to) over the raw
    # extracted field, which is null whenever the resume never states an
    # explicit "X years" figure even though its dates make one computable.
    # Nothing counted (no roles, or only uncredited internships) is 0 years,
    # not missing data. Internships count as the JD's calibration says.
    candidate_total_years = experience_years_for_scoring(parsed_json, jd_internship_credit(job_description))
    candidate_education_entries = parsed_json.get("education")
    required_degree_text = (job_description.education_criteria or {}).get("degree")
    jd_extracted_education = (job_description.extracted_json or {}).get("education")

    weight_configs = config_repo.get_configs_by_keys([
        _EXPERIENCE_TOLERANCE_YEARS_KEY,
        _EQUIVALENT_EXPERIENCE_YEARS_KEY,
        _DETERMINISTIC_WEIGHT_SKILLS_KEY,
        _DETERMINISTIC_WEIGHT_EXPERIENCE_KEY,
        _DETERMINISTIC_WEIGHT_EDUCATION_KEY,
        _DETERMINISTIC_WEIGHT_FUNCTIONAL_KEY,
    ])
    validation_service = ExperienceEducationValidationService(
        experience_tolerance_years=float(weight_configs.get(_EXPERIENCE_TOLERANCE_YEARS_KEY, 0.0)),
        equivalent_experience_years=float(weight_configs[_EQUIVALENT_EXPERIENCE_YEARS_KEY])
        if weight_configs.get(_EQUIVALENT_EXPERIENCE_YEARS_KEY) is not None else None,
    )
 
    min_experience_years = (
        float(job_description.min_experience_years)
        if job_description.min_experience_years is not None else None
    )
    # JD calibration: the calibrated minimum first, else the extraction / column.
    min_experience_years, jd_extracted_experience = experience_minimum_inputs(job_description, min_experience_years)
    experience_result = validation_service.validate_experience(
        min_experience_years, candidate_total_years,
        jd_extracted_experience=jd_extracted_experience,
    )
    education_result = validation_service.validate_education(
        required_degree_text, candidate_education_entries, candidate_total_years,
        jd_extracted_education=jd_extracted_education,
        required_field_text=(job_description.education_criteria or {}).get("field"),
        # Related fields for the JD's role domain, when its education is mandatory.
        field_families=field_families_for_jd(config_repo.db, job_description),
    )
    # JD calibration: the near-miss tolerance and education mandatory/preferred.
    experience_result = apply_experience_tolerance(experience_result, job_description)
    education_result = apply_education_requirement(education_result, job_description)
    score_weights = {
        "skills": float(weight_configs.get(_DETERMINISTIC_WEIGHT_SKILLS_KEY, 0.70)),
        "experience": float(weight_configs.get(_DETERMINISTIC_WEIGHT_EXPERIENCE_KEY, 0.15)),
        "education": float(weight_configs.get(_DETERMINISTIC_WEIGHT_EDUCATION_KEY, 0.15)),
        "functional": float(weight_configs.get(_DETERMINISTIC_WEIGHT_FUNCTIONAL_KEY, 0.15)),
    }
    domain_result = DomainCapabilityMatchingService().evaluate(job_description.extracted_json, parsed_json)
    return experience_result, education_result, score_weights, domain_result


class ScoringV2DecisionPending(Exception):
    """v2 decides this campaign but its confirmation couldn't run - retry the task, never reject."""


def _run_scoring_v2(db, skill_repo, config_repo, campaign, campaign_candidate, job_description, resume,
                    experience_result, education_result, domain_result, *, acted_on: bool,
                    llm_fallback: str = FALLBACK_NEVER):
    """Inline v2 (only when it decides) - breakdown, or None when it fails (logged, never raised)."""
    try:
        from app.services.llm.factory import resolve_active_provider
        from app.services.llm.model_selection import LLMPurpose
        return ScoringV2Service(
            db, skill_repo, config_repo,
            provider_resolver=lambda: resolve_active_provider(db, LLMPurpose.EXTRACTION),
        ).score(
            campaign=campaign, campaign_candidate=campaign_candidate, job_description=job_description,
            resume=resume, experience_result=experience_result, education_result=education_result,
            domain_result=domain_result, acted_on=acted_on, llm_fallback=llm_fallback,
        )
    except Exception:
        logger.exception("Scoring v2 failed | campaign_candidate_id=%s", campaign_candidate.id)
        return None


def store_v2_decision(campaign_candidate, v2_breakdown: dict) -> dict:
    """
    v2's decision on the candidate: its own breakdown, score and pass flag, stored as they are
    (retire v1, phase C - before, they were copied into v1's breakdown, v2's nested under "v2").
    """
    breakdown = dict(v2_breakdown)
    campaign_candidate.deterministic_breakdown = breakdown
    campaign_candidate.deterministic_score = breakdown["deterministic_score"]
    campaign_candidate.deterministic_passed = breakdown["deterministic_passed"]
    return breakdown


AI_EVALUATE_TASK_TYPE = "AI_EVALUATE"
_AI_EVALUATION_SKIPPED_ERROR_MESSAGE = "Candidate rejected at DETERMINISTIC layer — AI evaluation skipped."


def _cancel_downstream_ai_evaluation(
    campaign_candidate,
    task_log_repo: CeleryTaskLogRepository,
    task_log_service: CeleryTaskLogService,
    ai_evaluation_repo: CampaignCandidateAIEvaluationRepository,
) -> None:
    """
    Bug fix: previously returned early (no SKIPPED write at all) when no
    QUEUED AI_EVALUATE task existed - which is the common case, since
    AI_EVALUATE is only ever queued after a semantic PASS
    (semantic_scoring_tasks.py's _enqueue_ai_evaluation). A candidate
    rejected at DETERMINISTIC or SEMANTIC on its first pass through never
    had an AI_EVALUATE row to cancel, so this always short-circuited and
    the candidate's ai_evaluation_status was left at whatever get_or_create's
    default is (PENDING) - indistinguishable from "not processed yet" on
    the AI evaluation tab. Now always marks SKIPPED, whether or not there
    was anything queued to cancel.
    """
    queued_ai_evaluate_logs = [
        log for log in task_log_repo.get_by_campaign_candidate_and_task_type(
            campaign_candidate.id, AI_EVALUATE_TASK_TYPE,
        )
        if log.status == TaskStatus.QUEUED
    ]
    for log in queued_ai_evaluate_logs:
        task_log_service.mark_dead(log, _AI_EVALUATION_SKIPPED_ERROR_MESSAGE)
        logger.info(
            "Cancelled queued AI_EVALUATE task | task_id=%s campaign_candidate_id=%s",
            log.task_id, campaign_candidate.id,
        )

    ai_evaluation = ai_evaluation_repo.get_or_create(campaign_candidate.id)
    ai_evaluation.ai_evaluation_status = AIEvaluationStatus.SKIPPED
    ai_evaluation_repo.update(ai_evaluation)


@celery_app.task(name="scoring.calculate_deterministic_score", bind=True)
def calculate_deterministic_score_task(self, campaign_candidate_id: str, score_only: bool = False) -> None:
    """
    score_only (re-score of a candidate past Screening, or Rejected without a
    recruiter's move - see resume_reparse_service.request_rescore): scores are
    recomputed and stored with the previous ones recorded, but the stage never
    changes and nothing downstream (semantic / AI) is queued.
    """
   
    db = SessionLocal()
    task_log = None
    task_id = self.request.id
    try:
        campaign_candidate_repo = CampaignCandidateRepository(db)
        campaign_repo = CampaignRepository(db)
        resume_repo = ResumeRepository(db)
        jd_repo = JDRepository(db)
        skill_repo = SkillRepository(db)
        config_repo = ConfigRepository(db)
        ai_evaluation_repo = CampaignCandidateAIEvaluationRepository(db)
        allowed_transition_repo = AllowedTransitionRepository(db)
        audit_service = AuditService(AuditRepository(db))
        task_log_repo = CeleryTaskLogRepository(db)
        task_log_service = CeleryTaskLogService(task_log_repo)
        interview_schedule_repo = InterviewScheduleRepository(db)
        stage_transition_service = StageTransitionService(
            allowed_transition_repo, campaign_candidate_repo, audit_service, interview_schedule_repo,
        )


        campaign_candidate = campaign_candidate_repo.get_by_id(UUID(campaign_candidate_id))

        existing_task_log = task_log_repo.get_by_task_id(task_id)

        
        if existing_task_log is not None and existing_task_log.status == TaskStatus.SUCCESS:
            logger.info(
                "Deterministic scoring already completed for task_id=%s campaign_candidate_id=%s "
                "- skipping duplicate run.", task_id, campaign_candidate_id,
            )
            return

        if existing_task_log is None:
            existing_task_log = task_log_service.create_log(
                task_id=task_id,
                task_type=DETERMINISTIC_SCORE_TASK_TYPE,
               
                campaign_candidate_id=campaign_candidate.id if campaign_candidate is not None else None,
            )
        task_log = task_log_service.mark_running(existing_task_log)

        if campaign_candidate is None:
            summary = json.dumps({
                "skipped": True,
                "reason": f"campaign_candidate_id {campaign_candidate_id} no longer exists.",
            })
            task_log_service.mark_success(task_log, summary=summary)
            logger.warning(
                "Deterministic scoring skipped | campaign_candidate_id=%s reason=campaign_candidate_deleted "
                "(candidate was likely removed from the campaign while its resume was still processing).",
                campaign_candidate_id,
            )
            return

        campaign = campaign_repo.get_by_id(campaign_candidate.campaign_id)
        if campaign is None:
            raise ValueError(f"Campaign '{campaign_candidate.campaign_id}' not found.")

        if campaign.status not in _SCOREABLE_CAMPAIGN_STATUSES:
            summary = json.dumps({"skipped": True, "reason": f"Campaign status is {campaign.status.value}."})
            task_log_service.mark_success(task_log, summary=summary)
            logger.info(
                "Deterministic scoring skipped | campaign_candidate_id=%s reason=campaign_status_%s",
                campaign_candidate_id, campaign.status.value,
            )
            return

        resume = resume_repo.get_by_id(campaign_candidate.resume_id)
        if resume is None or resume.parse_status != ParseStatus.PARSED:
            raise ValueError(
                f"Resume '{campaign_candidate.resume_id}' has not completed parsing/skill "
                "normalization yet - deterministic scoring cannot run."
            )

        job_description = jd_repo.get_by_id(campaign.jd_id)
        if job_description is None:
            raise ValueError(f"Job description '{campaign.jd_id}' not found.")

        if not score_only:
            stage_transition_service.transition_to_screening(campaign_candidate)
        # transition_to_screening()'s return value used to be discarded here -
        # its own no-op branches are meant to be benign (candidate already
        # progressed past UPLOADED, e.g. a redelivered duplicate task), but a
        # genuine failure (e.g. no allowed_transitions row for UPLOADED ->
        # SCREENING) is *also* a silent no-op from its return value alone,
        # and scoring would proceed anyway - persisting a real score against
        # a candidate still stuck at UPLOADED, with the task marked SUCCESS
        # and never retried. Checking pipeline_stage after the call
        # distinguishes the two: if it's still UPLOADED, the transition
        # genuinely did not apply, which is never safe to score through
        # silently - raising here routes it through this task's normal
        # failure handling (task_log marked FAILED, visible for retry)
        # instead of a silent, permanent stuck state.
        if not score_only and campaign_candidate.pipeline_stage == PipelineStage.UPLOADED:
            raise RuntimeError(
                f"transition_to_screening did not advance campaign_candidate {campaign_candidate.id} "
                "past UPLOADED - deterministic scoring cannot safely proceed. Check allowed_transitions "
                "for an UPLOADED -> SCREENING row."
            )


        experience_result, education_result, _score_weights, domain_result = build_validation_inputs(
            config_repo, job_description, resume,
        )
        previous_score = getattr(campaign_candidate, "deterministic_score", None)
        previous_score = float(previous_score) if previous_score is not None else None
        previous_passed = getattr(campaign_candidate, "deterministic_passed", None)

        # Scoring v2 decides every campaign (retire v1, phase C - v1 no longer runs). The daily cap
        # falls back to no-LLM scoring at once (it won't lift for hours); a provider failure is
        # retried, and falls back only on the last attempt. v2 failing outright is retried too, and
        # dead-lettered after the last attempt - never decided on partial information.
        last_attempt = self.request.retries + 1 >= _DECISION_PENDING_RETRY_POLICY.max_attempts
        breakdown = _run_scoring_v2(
            db, skill_repo, config_repo, campaign, campaign_candidate, job_description, resume,
            experience_result, education_result, domain_result, acted_on=True,
            llm_fallback=FALLBACK_ON_UNAVAILABLE if last_attempt else FALLBACK_ON_CAP,
        )
        if breakdown is None:
            raise ScoringV2DecisionPending("v2 scoring failed (see the worker log)")
        if breakdown.get("outcome") in _UNDECIDED_OUTCOMES:
            # A provider failure (PENDING) or the daily cap (NOT_RUN) must never decide (and
            # never reject - NOT_RUN carries deterministic_passed=None): roll back and retry
            # the whole scoring later.
            raise ScoringV2DecisionPending(
                f"{breakdown.get('outcome')}: {breakdown.get('outcome_reason') or 'v2 confirmation unavailable'}"
            )
        store_v2_decision(campaign_candidate, breakdown)

        now = datetime.now(timezone.utc)
        # Scored now with the resume's current skills - no longer out of date.
        campaign_candidate.score_out_of_date_at = None
        campaign_candidate.score_out_of_date_reason = None
        campaign_candidate.screened_at = now
        campaign_candidate.updated_at = now
        campaign_candidate_repo.update(campaign_candidate)

        missing_entries = [skill for skill in breakdown["mandatory_skills"] if skill["match_type"] == "MISSING"]

        
        rejection_reason = None
        stage_transition_succeeded = False
        if not breakdown["deterministic_passed"] and not score_only:

            rejection_reason = v2_rejection_reason(breakdown)

            stage_transition_succeeded = stage_transition_service.transition_to_rejected(
                campaign_candidate,
                change_reason="Deterministic filter rejection",
                scores_snapshot=breakdown,
                decision_source=DecisionSource.DETERMINISTIC,
                decision_reason=rejection_reason,
                decision_details=breakdown,
            )

            _cancel_downstream_ai_evaluation(
                campaign_candidate, task_log_repo, task_log_service, ai_evaluation_repo,
            )

        matched_count = len(breakdown["mandatory_skills"]) - len(missing_entries)
        summary_payload = {
            "mandatory_skills_checked": len(breakdown["mandatory_skills"]),
            "matched": matched_count,
            "missing": len(missing_entries),
            "core_skill_count": breakdown.get("core_skill_count"),
            "core_confirmed_count": breakdown.get("core_confirmed_count"),
            "core_coverage_pct": breakdown.get("core_coverage_pct"),
            "missing_core_skill_count": breakdown.get("missing_core_skill_count"),
            "allowed_missing_core_skills": breakdown.get("allowed_missing_core_skills"),
            "outcome": breakdown.get("outcome"),
            "scoring_mode": breakdown.get("scoring_mode"),
            "deterministic_score": breakdown["deterministic_score"],
            "deterministic_passed": breakdown["deterministic_passed"],
            "rejection_reason": rejection_reason,
            # Never the breakdown: it quotes the resume (evidence lines, vote quotes,
            # the education field). It is stored on the candidate (decision_details).
        }
        if score_only:
            # What changed - the stage is left as it is.
            summary_payload.update(score_only=True, stage_unchanged=campaign_candidate.pipeline_stage.value,
                                   previous_deterministic_score=previous_score,
                                   previous_deterministic_passed=previous_passed)

       
        audit_service.log(
            actor_id=None,
            actor_role="SYSTEM",
            action_type=ActionType.DETERMINISTIC_SCORE_COMPUTED,
            entity_type=EntityType.CAMPAIGN_CANDIDATE,
            entity_id=campaign_candidate.id,
            campaign_id=campaign.id,
            details=summary_payload,
        )

        campaign_candidate_repo.commit()

        CandidateChangeNotifier().updated(campaign.id, campaign_candidate.id, campaign_candidate.resume_id)

        task_log_service.mark_success(task_log, summary=json.dumps(summary_payload))

        logger.info(
            "Deterministic scoring completed | campaign_candidate_id=%s deterministic_passed=%s",
            campaign_candidate.id, breakdown["deterministic_passed"],
        )


        # A system rejection never emails the candidate - a recruiter sends
        # the rejection email manually (POST .../send-rejection-email).
        if not breakdown["deterministic_passed"] and stage_transition_succeeded:
            try:
                _enqueue_composite_scoring(
                    campaign_candidate.id, task_log_service, CompositeScoreTriggerSource.REJECTION,
                )
            except Exception:
                logger.exception(
                    "Failed to enqueue composite scoring after deterministic rejection for campaign_candidate_id=%s",
                    campaign_candidate.id,
                )

        if breakdown["deterministic_passed"] and not score_only:
            try:
                from app.tasks.semantic_scoring_tasks import _enqueue_semantic_scoring
                _enqueue_semantic_scoring(campaign_candidate, task_log_service, resume_repo, jd_id=campaign.jd_id)
            except Exception:
                logger.exception(
                    "Failed to enqueue semantic scoring after deterministic pass for campaign_candidate_id=%s",
                    campaign_candidate.id,
                )

    except ScoringV2DecisionPending as ex:
        db.rollback()
        _retry_or_dead_letter(self, db, task_log_service, task_log, campaign_candidate_id, score_only, ex)
    except Exception as ex:
        db.rollback()
        if task_log:
            task_log_service.mark_failure(task_log, str(ex))
        logger.exception("Deterministic scoring task failed for campaign_candidate_id %s", campaign_candidate_id)
        raise
    finally:
        db.close()


def _retry_or_dead_letter(task, db, task_log_service, task_log, campaign_candidate_id, score_only, ex) -> None:
    """v2 couldn't decide: retry with backoff; after the last attempt, a replayable dead-letter row."""
    attempt_number = task.request.retries + 1
    if attempt_number < _DECISION_PENDING_RETRY_POLICY.max_attempts:
        if task_log:
            task_log_service.mark_retry(task_log)
        delay = compute_backoff_seconds(_DECISION_PENDING_RETRY_POLICY, attempt_number)
        logger.warning("Scoring decision pending, retrying | campaign_candidate_id=%s attempt=%s delay=%ss: %s",
                       campaign_candidate_id, attempt_number, delay, ex)
        task.retry(exc=ex, countdown=delay, max_retries=_DECISION_PENDING_RETRY_POLICY.max_attempts)
        return
    try:
        dead_letters = DeadLetterQueueRepository(db)
        dead_letters.create(
            original_task_id=task.request.id, task_type=DETERMINISTIC_SCORE_TASK_TYPE,
            final_error_message=f"Scoring decision pending after {attempt_number} attempts: {ex}"[:2000],
            input_payload={"campaign_candidate_id": campaign_candidate_id, "score_only": bool(score_only)},
            retry_count=attempt_number,
            first_attempted_at=getattr(task_log, "queued_at", None) or datetime.now(timezone.utc),
            last_attempted_at=datetime.now(timezone.utc), campaign_candidate_id=UUID(campaign_candidate_id),
        )
        dead_letters.commit()
    except Exception:
        logger.exception("Failed to dead-letter DETERMINISTIC_SCORE for campaign_candidate_id=%s", campaign_candidate_id)
        db.rollback()
    if task_log:
        task_log_service.mark_dead(task_log, str(ex))
    logger.error("Scoring decision still pending - dead-lettered | campaign_candidate_id=%s: %s",
                 campaign_candidate_id, ex)


def _read_recovery_hours(config_repo) -> float:
    raw = config_repo.get_configs_by_keys([_RECOVERY_HOURS_KEY]).get(_RECOVERY_HOURS_KEY)
    try:
        return float(raw) if raw is not None else _DEFAULT_RECOVERY_HOURS
    except (TypeError, ValueError):
        logger.warning("Invalid %s value %r - using %s.", _RECOVERY_HOURS_KEY, raw, _DEFAULT_RECOVERY_HOURS)
        return _DEFAULT_RECOVERY_HOURS


def recovery_action(logs, now: datetime, recovery_hours: float) -> str:
    """
    For one never-screened candidate, from its DETERMINISTIC_SCORE task logs: "in_flight" (one is
    queued / running / waiting to retry), "recent" (the last attempt ended under recovery_hours
    ago - e.g. dead-lettered on the daily cap, which only a reset fixes), else "requeue".
    """
    if any(log.status in _IN_FLIGHT for log in logs):
        return "in_flight"
    ends = [log.completed_at or log.started_at or log.queued_at for log in logs]
    ends = [end for end in ends if end is not None]
    if ends and now - max(ends) < timedelta(hours=recovery_hours):
        return "recent"
    return "requeue"


@celery_app.task(name="scoring.recover_undecided_candidates")
def recover_undecided_candidates() -> None:
    """
    Hourly safety net: candidates whose screening decision never landed (still UPLOADED or
    SCREENING, never screened, resume parsed, campaign scoreable) - a v2 decision that stayed
    PENDING / NOT_RUN through every retry, or scoring that was never queued - are queued again,
    at most once per SCORING_DECISION_RECOVERY_HOURS (seeded 6) each, never twice at once.
    """
    db = SessionLocal()
    task_log = None
    task_log_service = CeleryTaskLogService(CeleryTaskLogRepository(db))
    try:
        task_log = task_log_service.mark_running(
            task_log_service.create_log(task_id=str(uuid4()), task_type=DECISION_RECOVERY_TASK_TYPE))
        now = datetime.now(timezone.utc)
        recovery_hours = _read_recovery_hours(ConfigRepository(db))
        task_log_repo = task_log_service.repository
        counts = {"found": 0, "requeued": 0, "in_flight": 0, "recent": 0}
        for campaign_candidate in CampaignCandidateRepository(db).get_never_screened(
                _SCOREABLE_CAMPAIGN_STATUSES, created_before=now - _RECOVERY_GRACE):
            counts["found"] += 1
            logs = task_log_repo.get_by_campaign_candidate_and_task_type(
                campaign_candidate.id, DETERMINISTIC_SCORE_TASK_TYPE)
            action = recovery_action(logs, now, recovery_hours)
            if action != "requeue":
                counts[action] += 1
                continue
            scoring_task_id = str(uuid4())
            task_log_service.create_log(task_id=scoring_task_id, task_type=DETERMINISTIC_SCORE_TASK_TYPE,
                                        campaign_candidate_id=campaign_candidate.id)
            calculate_deterministic_score_task.apply_async(
                kwargs={"campaign_candidate_id": str(campaign_candidate.id)}, task_id=scoring_task_id)
            counts["requeued"] += 1
        task_log_service.mark_success(task_log, summary=json.dumps(counts))
        logger.info("Scoring decision recovery | %s", counts)
    except Exception as ex:
        db.rollback()
        if task_log:
            task_log_service.mark_failure(task_log, str(ex))
        logger.exception("Scoring decision recovery scan failed")
    finally:
        db.close()
