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
from app.models.pipeline import AIEvaluationStatus, CandidateRejection, RejectionLayer
from app.repositories.allowed_transition_repository import AllowedTransitionRepository
from app.repositories.audit_repository import AuditRepository
from app.repositories.campaign_candidate_repository import CampaignCandidateRepository
from app.repositories.CampaignRepository import CampaignRepository
from app.repositories.candidate_rejection_repository import CandidateRejectionRepository
from app.repositories.celery_task_log_repository import CeleryTaskLogRepository
from app.repositories.config_repository import ConfigRepository
from app.repositories.email_notification_repository import EmailNotificationRepository
from app.repositories.email_template_repository import EmailTemplateRepository
from app.repositories.jd_repository import JDRepository
from app.repositories.resume_repository import ResumeRepository
from app.repositories.skill_ontology_repository import SkillOntologyRepository
from app.repositories.skill_repository import SkillRepository
from app.services.audit_service import AuditService
from app.services.campaign.candidate_scoring_service import (
    CandidateScoringService,
    MandatorySkillMatchType,
)
from app.services.campaign.experience_education_validation_service import (
    ExperienceEducationValidationService,
)
from app.services.campaign.stage_transition_service import StageTransitionService
from app.services.celery_task_log_service import CeleryTaskLogService
from app.services.notifications.candidate_rejection_email_service import CandidateRejectionEmailService
from app.tasks.email_tasks import send_candidate_email_task

logger = logging.getLogger(__name__)

DETERMINISTIC_SCORE_TASK_TYPE = "DETERMINISTIC_SCORE"

# Campaign states this task will actually score against - a CLOSED campaign
# is a legitimate reason to skip, not a failure (M07-E01 S02 T02 rule 2).
_SCOREABLE_CAMPAIGN_STATUSES = {CampaignStatus.ACTIVE, CampaignStatus.PAUSED}

# M07-E02 S04: platform_config keys for the combined deterministic score.
# Missing/unconfigured keys fall back to ExperienceEducationValidationService's
# and CandidateScoringService's own module defaults - never hard-fail.
_EXPERIENCE_TOLERANCE_YEARS_KEY = "EXPERIENCE_TOLERANCE_YEARS"
_EQUIVALENT_EXPERIENCE_YEARS_KEY = "EQUIVALENT_EXPERIENCE_YEARS"
_DETERMINISTIC_WEIGHT_SKILLS_KEY = "DETERMINISTIC_WEIGHT_SKILLS"
_DETERMINISTIC_WEIGHT_EXPERIENCE_KEY = "DETERMINISTIC_WEIGHT_EXPERIENCE"
_DETERMINISTIC_WEIGHT_EDUCATION_KEY = "DETERMINISTIC_WEIGHT_EDUCATION"

# M07-E03 S01 T03: task_type this task's rejection must cancel if still
# QUEUED. Not yet produced by anything in this codebase (M09 AI Evaluation
# isn't built) - this check is a no-op today and activates automatically
# once that task exists, without requiring any further change here.
AI_EVALUATE_TASK_TYPE = "AI_EVALUATE"
_AI_EVALUATION_SKIPPED_ERROR_MESSAGE = "Candidate rejected at DETERMINISTIC layer — AI evaluation skipped."


def _cancel_downstream_ai_evaluation(
    campaign_candidate,
    task_log_repo: CeleryTaskLogRepository,
    task_log_service: CeleryTaskLogService,
    campaign_candidate_repo: CampaignCandidateRepository,
) -> None:
    """
    M07-E03 S01 T03: a candidate rejected at the DETERMINISTIC layer must
    never have a queued AI_EVALUATE task run against it. Only QUEUED
    AI_EVALUATE logs for THIS campaign_candidate_id are touched - never
    other candidates, never other campaigns. EMBED_RESUME is deliberately
    never inspected or touched here: embedding generation is independent of
    the deterministic outcome and left to run to completion regardless.
    """
    queued_ai_evaluate_logs = [
        log for log in task_log_repo.get_by_campaign_candidate_and_task_type(
            campaign_candidate.id, AI_EVALUATE_TASK_TYPE,
        )
        if log.status == TaskStatus.QUEUED
    ]
    if not queued_ai_evaluate_logs:
        return

    for log in queued_ai_evaluate_logs:
        task_log_service.mark_dead(log, _AI_EVALUATION_SKIPPED_ERROR_MESSAGE)
        logger.info(
            "Cancelled queued AI_EVALUATE task | task_id=%s campaign_candidate_id=%s",
            log.task_id, campaign_candidate.id,
        )

    campaign_candidate.ai_evaluation_status = AIEvaluationStatus.SKIPPED
    campaign_candidate_repo.update(campaign_candidate)


def _queue_rejection_email(db, campaign_candidate) -> None:
    """
    M07-E03 S02 T02: queues the CANDIDATE_REJECTED email, but only ever
    called after the scoring/rejection/stage-transition transaction has
    already committed (see the call site) - a failure here must never
    crash or mask that already-successful outcome, same reasoning as
    _enqueue_deterministic_scoring's own try/except in
    resume_processing_tasks.py.
    """
    try:
        email_template_repo = EmailTemplateRepository(db)
        email_notification_repo = EmailNotificationRepository(db)
        email_service = CandidateRejectionEmailService(email_template_repo, email_notification_repo)

        notification = email_service.queue_rejection_email(
            candidate_id=campaign_candidate.candidate_id,
            campaign_candidate_id=campaign_candidate.id,
        )
        if notification is None:
            return  # already logged inside queue_rejection_email (no active template)

        send_candidate_email_task.apply_async(
            kwargs={"email_notification_id": str(notification.id)},
        )
    except Exception:
        logger.exception(
            "Failed to queue rejection email for campaign_candidate_id=%s", campaign_candidate.id,
        )


@celery_app.task(name="scoring.calculate_deterministic_score", bind=True)
def calculate_deterministic_score_task(self, campaign_candidate_id: str) -> None:
    """
    Hierarchy-aware deterministic mandatory-skill scoring for one
    campaign_candidate, enqueued after resume processing (skill
    normalization + candidate_skills commit) completes successfully -
    see process_resume_document's enqueue hook. Reuses the existing,
    already-tested CandidateScoringService as-is; this task only wires
    validation, persistence, rejection, task-log and audit-log around it.
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
        candidate_rejection_repo = CandidateRejectionRepository(db)
        allowed_transition_repo = AllowedTransitionRepository(db)
        audit_service = AuditService(AuditRepository(db))
        task_log_repo = CeleryTaskLogRepository(db)
        task_log_service = CeleryTaskLogService(task_log_repo)
        stage_transition_service = StageTransitionService(allowed_transition_repo, campaign_candidate_repo)

        # Root cause: _enqueue_deterministic_scoring captures a valid
        # campaign_candidate_id at enqueue time, but resume processing (AI
        # extraction + embedding generation) can run for a long time, and
        # Celery delivery/pickup can itself be delayed - an HR admin can
        # legitimately delete_campaign_candidate that same candidate in the
        # meantime. Existence must be checked BEFORE any write that has a
        # hard FK dependency on it (the CeleryTaskLog row below) - creating
        # that row first, as before, meant a since-deleted candidate raised
        # psycopg2.errors.ForeignKeyViolation instead of a graceful skip.
        campaign_candidate = campaign_candidate_repo.get_by_id(UUID(campaign_candidate_id))

        existing_task_log = task_log_repo.get_by_task_id(task_id)

        # M07-E03 S01 T01: a broker redelivery of this exact task_id after
        # it already ran to completion must never re-run scoring or insert
        # a second candidate_rejections row for the same evaluation. A
        # FAILURE/RUNNING log is still reprocessed (the work never actually
        # finished), only SUCCESS short-circuits.
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
                # Only set when campaign_candidate genuinely exists - this is
                # exactly the FK the existence check above protects against
                # violating.
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

        # M07-E02 S01/S02: read whatever the AI extraction pipeline already
        # captured on the resume - no new columns, this is the same
        # parsed_json ResumeExtractionResponse.model_dump() already wrote.
        parsed_json = resume.parsed_json or {}
        candidate_total_years = parsed_json.get("total_experience_years")
        candidate_education_entries = parsed_json.get("education")
        required_degree_text = (job_description.education_criteria or {}).get("degree")

        weight_configs = config_repo.get_configs_by_keys([
            _EXPERIENCE_TOLERANCE_YEARS_KEY,
            _EQUIVALENT_EXPERIENCE_YEARS_KEY,
            _DETERMINISTIC_WEIGHT_SKILLS_KEY,
            _DETERMINISTIC_WEIGHT_EXPERIENCE_KEY,
            _DETERMINISTIC_WEIGHT_EDUCATION_KEY,
        ])
        validation_service = ExperienceEducationValidationService(
            experience_tolerance_years=float(weight_configs.get(_EXPERIENCE_TOLERANCE_YEARS_KEY, 0.0)),
            equivalent_experience_years=float(weight_configs[_EQUIVALENT_EXPERIENCE_YEARS_KEY])
            if weight_configs.get(_EQUIVALENT_EXPERIENCE_YEARS_KEY) is not None else None,
        )
        # job_description.min_experience_years is a Numeric(4,1) column -
        # SQLAlchemy returns a decimal.Decimal, which cannot be subtracted
        # from ExperienceEducationValidationService's float tolerance
        # (TypeError: unsupported operand type(s) for -: 'decimal.Decimal'
        # and 'float'). Cast at the DB boundary, same as every other
        # Numeric column already crossing into scoring elsewhere in this
        # codebase (e.g. candidate_scoring_service.py's `float(row.weight)`).
        min_experience_years = (
            float(job_description.min_experience_years)
            if job_description.min_experience_years is not None else None
        )
        experience_result = validation_service.validate_experience(
            min_experience_years, candidate_total_years,
        )
        education_result = validation_service.validate_education(
            required_degree_text, candidate_education_entries, candidate_total_years,
        )
        score_weights = {
            "skills": float(weight_configs.get(_DETERMINISTIC_WEIGHT_SKILLS_KEY, 0.70)),
            "experience": float(weight_configs.get(_DETERMINISTIC_WEIGHT_EXPERIENCE_KEY, 0.15)),
            "education": float(weight_configs.get(_DETERMINISTIC_WEIGHT_EDUCATION_KEY, 0.15)),
        }

        scoring_service = CandidateScoringService(
            skill_repo, skill_ontology_repo, config_repo, campaign_candidate_repo,
        )
        breakdown = scoring_service.calculate_and_store_score_breakdown(
            campaign_candidate.id, campaign.jd_id, campaign_candidate.resume_id,
            float(campaign.deterministic_threshold),
            experience_result=experience_result,
            education_result=education_result,
            score_weights=score_weights,
        )

        now = datetime.now(timezone.utc)
        campaign_candidate.screened_at = now
        campaign_candidate.updated_at = now
        campaign_candidate_repo.update(campaign_candidate)

        missing_entries = [
            skill for skill in breakdown["mandatory_skills"]
            if skill["match_type"] == MandatorySkillMatchType.MISSING.value
        ]

        # M07-E03 S01: a rejection must be recorded on ANY deterministic
        # failure, not only when a mandatory skill is outright MISSING - a
        # candidate whose mandatory skills all matched at low-multiplier
        # hierarchy tiers (SIBLING/SEMANTIC) can still fall short of
        # deterministic_threshold, and that case must not silently skip
        # candidate_rejections.
        rejection_reason = None
        stage_transition_succeeded = False
        if not breakdown["deterministic_passed"]:
            # T02: one dynamically-built, human-readable reason covering
            # every applicable failure (skills/experience/education),
            # never a database field name/UUID/internal code.
            rejection_reason = CandidateScoringService.build_rejection_reason(
                breakdown, experience_result, education_result,
            )
            # T01: rejection_detail is the complete score_breakdown snapshot
            # - not a curated per-branch subset - so nothing about this
            # evaluation is ever lost. The UPDATE (campaign_candidate,
            # flushed above) and this INSERT share the same uncommitted
            # transaction; the whole task's shared except-block rolls both
            # back together on any failure before the final commit below.
            candidate_rejection_repo.create(CandidateRejection(
                campaign_candidate_id=campaign_candidate.id,
                rejection_layer=RejectionLayer.DETERMINISTIC,
                rejection_reason=rejection_reason,
                rejection_detail=breakdown,
            ))

            # M07-E03 S02 T01: SCREENING -> REJECTED, validated against
            # allowed_transitions - if the transition is blocked (not
            # configured as allowed), pipeline_stage/stage_history are
            # left untouched and the candidate stays in SCREENING.
            stage_transition_succeeded = stage_transition_service.transition_to_rejected(
                campaign_candidate,
                change_reason="Deterministic filter rejection",
                scores_snapshot=breakdown,
            )

            # T03: a rejected candidate must never have a queued AI_EVALUATE
            # task run against them.
            _cancel_downstream_ai_evaluation(
                campaign_candidate, task_log_repo, task_log_service, campaign_candidate_repo,
            )

        matched_count = len(breakdown["mandatory_skills"]) - len(missing_entries)
        summary_payload = {
            "mandatory_skills_checked": len(breakdown["mandatory_skills"]),
            "matched": matched_count,
            "missing": len(missing_entries),
            # deterministic_score = (SUM mandatory contributions / SUM max
            # mandatory contributions) x 100 (M07) - mandatory skills only,
            # never includes the preferred_skill_bonus - the same value
            # persisted to campaign_candidate.deterministic_score.
            "deterministic_score": breakdown["deterministic_score"],
            "deterministic_passed": breakdown["deterministic_passed"],
            # M07-E03 S01 T02: the same human-readable reason recorded on
            # candidate_rejections, None when the candidate passed.
            "rejection_reason": rejection_reason,
            # Versioning: campaign_candidates.score_breakdown only ever holds
            # the latest computation and is overwritten on every rescoring.
            # This audit_log row is append-only and timestamped, so embedding
            # the full breakdown here (not just the summary above) reuses
            # the existing audit-log infrastructure as score_breakdown's
            # version history, with no schema change required.
            "score_breakdown": breakdown,
        }

        # Shares this task's db session - flushed here, committed together
        # with the campaign_candidate/rejection writes below (AuditRepository
        # itself has no .commit(); the last repository sharing the session
        # commits everything in one transaction).
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

        task_log_service.mark_success(task_log, summary=json.dumps(summary_payload))

        # M07-E03 S02 T02: only after the transaction above has committed -
        # never send a rejection email for a candidate whose pipeline_stage
        # didn't actually move to REJECTED (transition blocked - see T01).
        if not breakdown["deterministic_passed"] and stage_transition_succeeded:
            _queue_rejection_email(db, campaign_candidate)

        # M08-E02: only after the transaction above has committed - a
        # candidate that just passed deterministic screening is
        # auto-enqueued for semantic scoring, reusing the exact same
        # enqueue helper/idempotency logic
        # CampaignCandidateService._queue_post_override_evaluation already
        # uses (never a second/parallel implementation). Local import avoids
        # a circular import - semantic_scoring_tasks.py itself imports
        # _cancel_downstream_ai_evaluation from this module. A failure here
        # must never crash or mask the already-successful deterministic
        # outcome, same reasoning as _queue_rejection_email above.
        if breakdown["deterministic_passed"]:
            try:
                from app.tasks.semantic_scoring_tasks import _enqueue_semantic_scoring
                _enqueue_semantic_scoring(campaign_candidate, task_log_service, resume_repo)
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
