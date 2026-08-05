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


_SCOREABLE_CAMPAIGN_STATUSES = {CampaignStatus.ACTIVE, CampaignStatus.PAUSED}


_EXPERIENCE_TOLERANCE_YEARS_KEY = "EXPERIENCE_TOLERANCE_YEARS"
_EQUIVALENT_EXPERIENCE_YEARS_KEY = "EQUIVALENT_EXPERIENCE_YEARS"
_DETERMINISTIC_WEIGHT_SKILLS_KEY = "DETERMINISTIC_WEIGHT_SKILLS"
_DETERMINISTIC_WEIGHT_EXPERIENCE_KEY = "DETERMINISTIC_WEIGHT_EXPERIENCE"
_DETERMINISTIC_WEIGHT_EDUCATION_KEY = "DETERMINISTIC_WEIGHT_EDUCATION"


AI_EVALUATE_TASK_TYPE = "AI_EVALUATE"
_AI_EVALUATION_SKIPPED_ERROR_MESSAGE = "Candidate rejected at DETERMINISTIC layer — AI evaluation skipped."


def _cancel_downstream_ai_evaluation(
    campaign_candidate,
    task_log_repo: CeleryTaskLogRepository,
    task_log_service: CeleryTaskLogService,
    campaign_candidate_repo: CampaignCandidateRepository,
) -> None:
   
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

        
        rejection_reason = None
        stage_transition_succeeded = False
        if not breakdown["deterministic_passed"]:
           
            rejection_reason = CandidateScoringService.build_rejection_reason(
                breakdown, experience_result, education_result,
            )
          
            candidate_rejection_repo.create(CandidateRejection(
                campaign_candidate_id=campaign_candidate.id,
                rejection_layer=RejectionLayer.DETERMINISTIC,
                rejection_reason=rejection_reason,
                rejection_detail=breakdown,
            ))

           
            stage_transition_succeeded = stage_transition_service.transition_to_rejected(
                campaign_candidate,
                change_reason="Deterministic filter rejection",
                scores_snapshot=breakdown,
            )

           
            _cancel_downstream_ai_evaluation(
                campaign_candidate, task_log_repo, task_log_service, campaign_candidate_repo,
            )

        matched_count = len(breakdown["mandatory_skills"]) - len(missing_entries)
        summary_payload = {
            "mandatory_skills_checked": len(breakdown["mandatory_skills"]),
            "matched": matched_count,
            "missing": len(missing_entries),
          
            "deterministic_score": breakdown["deterministic_score"],
            "deterministic_passed": breakdown["deterministic_passed"],
            "rejection_reason": rejection_reason,
            "score_breakdown": breakdown,
        }

       
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

        logger.info(
            "Deterministic scoring completed | campaign_candidate_id=%s deterministic_passed=%s",
            campaign_candidate.id, breakdown["deterministic_passed"],
        )


        if not breakdown["deterministic_passed"] and stage_transition_succeeded:
            _queue_rejection_email(db, campaign_candidate)

        if breakdown["deterministic_passed"]:
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
