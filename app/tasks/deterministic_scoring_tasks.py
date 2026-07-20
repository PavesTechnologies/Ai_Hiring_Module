import json
import logging
from datetime import datetime, timezone
from uuid import UUID

from app.core.celery_app import celery_app
from app.db.session import SessionLocal
from app.enums.constants import ActionType, EntityType
from app.models.campaigns import CampaignStatus
from app.models.candidates import ParseStatus
from app.models.pipeline import CandidateRejection, RejectionLayer
from app.repositories.audit_repository import AuditRepository
from app.repositories.campaign_candidate_repository import CampaignCandidateRepository
from app.repositories.CampaignRepository import CampaignRepository
from app.repositories.candidate_rejection_repository import CandidateRejectionRepository
from app.repositories.celery_task_log_repository import CeleryTaskLogRepository
from app.repositories.config_repository import ConfigRepository
from app.repositories.resume_repository import ResumeRepository
from app.repositories.skill_ontology_repository import SkillOntologyRepository
from app.repositories.skill_repository import SkillRepository
from app.services.audit_service import AuditService
from app.services.campaign.candidate_scoring_service import (
    CandidateScoringService,
    MandatorySkillMatchType,
)
from app.services.celery_task_log_service import CeleryTaskLogService

logger = logging.getLogger(__name__)

DETERMINISTIC_SCORE_TASK_TYPE = "DETERMINISTIC_SCORE"

# Campaign states this task will actually score against - a CLOSED campaign
# is a legitimate reason to skip, not a failure (M07-E01 S02 T02 rule 2).
_SCOREABLE_CAMPAIGN_STATUSES = {CampaignStatus.ACTIVE, CampaignStatus.PAUSED}


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
        skill_repo = SkillRepository(db)
        skill_ontology_repo = SkillOntologyRepository(db)
        config_repo = ConfigRepository(db)
        candidate_rejection_repo = CandidateRejectionRepository(db)
        audit_service = AuditService(AuditRepository(db))
        task_log_repo = CeleryTaskLogRepository(db)
        task_log_service = CeleryTaskLogService(task_log_repo)

        existing_task_log = task_log_repo.get_by_task_id(task_id)
        if existing_task_log is None:
            existing_task_log = task_log_service.create_log(
                task_id=task_id,
                task_type=DETERMINISTIC_SCORE_TASK_TYPE,
                campaign_candidate_id=UUID(campaign_candidate_id),
            )
        task_log = task_log_service.mark_running(existing_task_log)

        campaign_candidate = campaign_candidate_repo.get_by_id(UUID(campaign_candidate_id))
        if campaign_candidate is None:
            raise ValueError(f"CampaignCandidate '{campaign_candidate_id}' not found.")

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

        scoring_service = CandidateScoringService(
            skill_repo, skill_ontology_repo, config_repo, campaign_candidate_repo,
        )
        breakdown = scoring_service.calculate_and_store_score_breakdown(
            campaign_candidate.id, campaign.jd_id, campaign_candidate.resume_id,
            float(campaign.deterministic_threshold),
        )

        now = datetime.now(timezone.utc)
        campaign_candidate.screened_at = now
        campaign_candidate.updated_at = now
        campaign_candidate_repo.update(campaign_candidate)

        missing_entries = [
            skill for skill in breakdown["mandatory_skills"]
            if skill["match_type"] == MandatorySkillMatchType.MISSING.value
        ]
        if missing_entries:
            missing_skill_names = []
            for entry in missing_entries:
                skill = skill_ontology_repo.get_skill_by_id(UUID(entry["canonical_skill_id"]))
                missing_skill_names.append(skill.canonical_name if skill else entry["canonical_skill_id"])

            candidate_rejection_repo.create(CandidateRejection(
                campaign_candidate_id=campaign_candidate.id,
                rejection_layer=RejectionLayer.DETERMINISTIC,
                rejection_reason="Missing mandatory skills",
                rejection_detail={"missing_skills": missing_skill_names},
            ))

        matched_count = len(breakdown["mandatory_skills"]) - len(missing_entries)
        summary_payload = {
            "mandatory_skills_checked": len(breakdown["mandatory_skills"]),
            "matched": matched_count,
            "missing": len(missing_entries),
            # deterministic_score is the final blended score (mandatory
            # coverage + preferred bonus, M03-E05 S01 T02) - the same value
            # persisted to campaign_candidate.deterministic_score.
            "deterministic_score": breakdown["deterministic_score"],
            "deterministic_passed": breakdown["deterministic_passed"],
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

    except Exception as ex:
        db.rollback()
        if task_log:
            task_log_service.mark_failure(task_log, str(ex))
        logger.exception("Deterministic scoring task failed for campaign_candidate_id %s", campaign_candidate_id)
        raise
    finally:
        db.close()
