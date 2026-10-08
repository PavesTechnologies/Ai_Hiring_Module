import json
import logging
from datetime import datetime, timezone
from uuid import UUID

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
from app.repositories.skill_ontology_repository import SkillOntologyRepository
from app.repositories.skill_repository import SkillRepository
from app.services.audit_service import AuditService
from app.services.campaign.candidate_scoring_service import (
    _optional_float,
    CandidateScoringService,
    MandatorySkillMatchType,
)
from app.services.campaign.domain_capability_matching_service import DomainCapabilityMatchingService
from app.repositories.scoring_result_repository import ScoringResultRepository
from app.services.campaign.scoring_v2 import OUTCOME_PENDING, REVIEW_OUTCOME_AVAILABLE
from app.services.campaign.scoring_v2_service import (
    ScoringV2Service, v1_result_row, v2_rejection_reason, v2_should_run,
)
from app.services.campaign.experience_education_validation_service import (
    ExperienceEducationValidationService,
)
from app.services.campaign.stage_transition_service import StageTransitionService
from app.services.celery_task_log_service import CeleryTaskLogService
from app.services.resume.work_experience_duration import annotate_work_experience_durations
from app.tasks.composite_scoring_tasks import _enqueue_composite_scoring
from app.services.candidate_change_notifier import CandidateChangeNotifier

logger = logging.getLogger(__name__)

DETERMINISTIC_SCORE_TASK_TYPE = "DETERMINISTIC_SCORE"


_SCOREABLE_CAMPAIGN_STATUSES = {CampaignStatus.ACTIVE, CampaignStatus.PAUSED}


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
    candidate_total_years = annotate_work_experience_durations(parsed_json).get("total_experience_years")
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
    jd_extracted_experience = (job_description.extracted_json or {}).get("experience")
    experience_result = validation_service.validate_experience(
        min_experience_years, candidate_total_years,
        jd_extracted_experience=jd_extracted_experience,
    )
    education_result = validation_service.validate_education(
        required_degree_text, candidate_education_entries, candidate_total_years,
        jd_extracted_education=jd_extracted_education,
        required_field_text=(job_description.education_criteria or {}).get("field"),
    )
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
                    experience_result, education_result, domain_result, *, acted_on: bool):
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
            domain_result=domain_result, acted_on=acted_on,
        )
    except Exception:
        logger.exception("Scoring v2 failed | campaign_candidate_id=%s", campaign_candidate.id)
        return None


def _record_v1_result(db, campaign, campaign_candidate, breakdown, *, acted_on: bool, score_weights) -> None:
    try:
        ScoringResultRepository(db).add(
            v1_result_row(campaign, campaign_candidate, breakdown, acted_on=acted_on, score_weights=score_weights)
        )
    except Exception:
        logger.exception("Could not store the v1 scoring result row | campaign_candidate_id=%s", campaign_candidate.id)


NEAR_MISS_BREAKDOWN_KEYS = (
    "near_miss_outcome", "needs_review", "needs_review_reason", "near_misses", "near_miss_reject_reasons",
)


def _act_on_v2(campaign_candidate, breakdown: dict, v2_breakdown: dict) -> dict:
    """
    scoring_v2_enabled: v2's score and decision drive the pipeline. The v1
    breakdown stays (the Deterministic tab reads its shape) with v1's own
    numbers kept beside v2's.
    """
    acted = dict(breakdown)
    acted.update(
        v1_deterministic_score=breakdown.get("deterministic_score"),
        v1_deterministic_passed=breakdown.get("deterministic_passed"),
        deterministic_score=v2_breakdown["deterministic_score"],
        deterministic_passed=v2_breakdown["deterministic_passed"],
        acted_on_version="v2",
        v2=v2_breakdown,
        # v2's REVIEW decision is the one the AI step's HOLD cap reads.
        **{key: v2_breakdown.get(key) for key in NEAR_MISS_BREAKDOWN_KEYS},
    )
    campaign_candidate.deterministic_breakdown = acted
    campaign_candidate.deterministic_score = v2_breakdown["deterministic_score"]
    campaign_candidate.deterministic_passed = v2_breakdown["deterministic_passed"]
    return acted


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
        skill_ontology_repo = SkillOntologyRepository(db)
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


        experience_result, education_result, score_weights, domain_result = build_validation_inputs(
            config_repo, job_description, resume,
        )

        scoring_service = CandidateScoringService(
            skill_repo, skill_ontology_repo, config_repo, campaign_candidate_repo,
        )
        previous_score = getattr(campaign_candidate, "deterministic_score", None)
        previous_score = float(previous_score) if previous_score is not None else None
        previous_passed = getattr(campaign_candidate, "deterministic_passed", None)
        breakdown = scoring_service.calculate_and_store_score_breakdown(
            campaign_candidate.id, campaign.jd_id, campaign_candidate.resume_id,
            float(campaign.deterministic_threshold),
            experience_result=experience_result,
            education_result=education_result,
            score_weights=score_weights,
            required_skill_coverage_threshold=float(campaign.required_skill_coverage_threshold),
            max_missing_core_skills=int(campaign.max_missing_core_skills),
            max_missing_core_pct=_optional_float(getattr(campaign, "max_missing_core_pct", None)),
            domain_result=domain_result,
        )

        # Scoring v2 (Phase 1). It decides only on scoring_v2_enabled campaigns
        # AND once the REVIEW outcome exists (go-live prerequisite) - then it
        # runs inline here. Everywhere else it opted into (shadow / enabled
        # before REVIEW) it runs afterwards in its own task (SCORING_V2),
        # queued after v1 commits: v1 is never slowed or changed by it.
        v2_enabled = getattr(campaign, "scoring_v2_enabled", False) is True
        v2_decides = v2_enabled and REVIEW_OUTCOME_AVAILABLE
        v2_breakdown = None
        if v2_decides:
            v2_breakdown = _run_scoring_v2(
                db, skill_repo, config_repo, campaign, campaign_candidate, job_description, resume,
                experience_result, education_result, domain_result, acted_on=True,
            )
            if v2_breakdown is not None and v2_breakdown.get("outcome") == OUTCOME_PENDING:
                # A quota/provider failure must never decide (and never reject):
                # roll back and retry the whole scoring later.
                raise ScoringV2DecisionPending(v2_breakdown.get("outcome_reason") or "v2 confirmation unavailable")
        elif v2_enabled:
            logger.warning("campaign %s has scoring_v2_enabled but the REVIEW outcome isn't available yet - "
                           "v2 runs in shadow only, v1 decides.", campaign.id)
        acting_on_v2 = v2_decides and v2_breakdown is not None
        _record_v1_result(db, campaign, campaign_candidate, breakdown,
                          acted_on=not acting_on_v2, score_weights=score_weights)
        if acting_on_v2:
            breakdown = _act_on_v2(campaign_candidate, breakdown, v2_breakdown)
        elif v2_decides:
            breakdown["v2_failed"] = True  # v2 errored: v1 decided this time (logged)

        now = datetime.now(timezone.utc)
        # Scored now with the resume's current skills - no longer out of date.
        campaign_candidate.score_out_of_date_at = None
        campaign_candidate.score_out_of_date_reason = None
        campaign_candidate.screened_at = now
        campaign_candidate.updated_at = now
        campaign_candidate_repo.update(campaign_candidate)

        missing_entries = [
            skill for skill in breakdown["mandatory_skills"]
            if skill["match_type"] == MandatorySkillMatchType.MISSING.value
        ]

        
        rejection_reason = None
        stage_transition_succeeded = False
        if not breakdown["deterministic_passed"] and not score_only:

            rejection_reason = (
                v2_rejection_reason(v2_breakdown) if acting_on_v2
                else CandidateScoringService.build_rejection_reason(breakdown, experience_result, education_result)
            )

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
            "mandatory_coverage_pct": breakdown["mandatory_coverage_pct"],
            "missing_core_skill_count": breakdown.get("missing_core_skill_count"),
            "max_missing_core_skills": breakdown.get("max_missing_core_skills"),
            "allowed_missing_core_skills": breakdown.get("allowed_missing_core_skills"),
            "skill_qualification_passed": breakdown.get("skill_qualification_passed"),
            "deterministic_score": breakdown["deterministic_score"],
            "deterministic_passed": breakdown["deterministic_passed"],
            "rejection_reason": rejection_reason,
            "score_breakdown": breakdown,
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

        if not acting_on_v2 and v2_should_run(campaign):
            try:
                from app.tasks.scoring_v2_tasks import enqueue_scoring_v2
                enqueue_scoring_v2(campaign_candidate, task_log_service)
            except Exception:
                logger.exception("Failed to queue shadow scoring v2 | campaign_candidate_id=%s", campaign_candidate.id)

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

    except Exception as ex:
        db.rollback()
        if task_log:
            task_log_service.mark_failure(task_log, str(ex))
        logger.exception("Deterministic scoring task failed for campaign_candidate_id %s", campaign_candidate_id)
        raise
    finally:
        db.close()
