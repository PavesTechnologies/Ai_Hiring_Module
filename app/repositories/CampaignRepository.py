from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session, joinedload
from app.models.pipeline import CampaignCandidate, PipelineStage
from datetime import datetime, timezone, timedelta

from app.schemas.campaign.campaign_filter_schema import CampaignFilterRequest
from app.schemas.campaign.campaign_schema import CampaignScoringUpdateRequest

from app.models.campaigns import CampaignStatus, HiringCampaign
from app.models.compliance import AuditLog
from app.models.skills import JDSkill
from app.models.pipeline import CampaignCandidate, CampaignCandidateStageHistory
from app.models.async_tasks import BulkUploadJob, BulkUploadStatus, CeleryTaskLog, TaskStatus
from app.models.identity import User, UserRole

class CampaignRepository:

    def __init__(self, db: Session):
        self.db = db

    def create_campaign(
        self,
        campaign: HiringCampaign,
    ) -> HiringCampaign:
        self.db.add(campaign)
        self.db.flush()
        self.db.refresh(campaign)
        return campaign

    def get_by_id(
        self,
        campaign_id: UUID,
    ) -> HiringCampaign | None:
        return (
            self.db.query(HiringCampaign)
            .filter(HiringCampaign.id == campaign_id)
            .first()
        )

    def get_scoring_configuration(
        self,
        campaign_id: UUID,
    ) -> HiringCampaign | None:
        """
        Fetch campaign scoring configuration.
        """
        return (
            self.db.query(HiringCampaign)
            .filter(HiringCampaign.id == campaign_id)
            .first()
        )
    def get_by_name(
        self,
        org_id: UUID,
        name: str,
    ) -> HiringCampaign | None:
        return (
            self.db.query(HiringCampaign)
            .filter(
                HiringCampaign.org_id == org_id,
                func.lower(HiringCampaign.name) == name.lower(),
            )
            .first()
        )

    def get_all_by_org(
        self,
        org_id: UUID,
    ) -> list[HiringCampaign]:
        return (
            self.db.query(HiringCampaign)
            .filter(HiringCampaign.org_id == org_id)
            .all()
        )
    
    def get_all_campaigns(self, show_closed: bool = False) -> list[HiringCampaign]:
        stmt = (
            select(HiringCampaign)
            # .where(
            #     HiringCampaign.status == "ACTIVE",
            # )
            .options(joinedload(HiringCampaign.job_description))
            .order_by(HiringCampaign.created_at.desc())
        )
        if not show_closed:
            stmt = stmt.where(
                HiringCampaign.status != CampaignStatus.CLOSED
            )
        result = self.db.execute(stmt)
        return result.scalars().all()
    
    def get_all_campaigns_for_hrAdmin(self, manager_id: UUID) -> list[HiringCampaign]:
        stmt = (
            select(HiringCampaign)
            .where(
                HiringCampaign.created_by == manager_id,
            )
            .options(joinedload(HiringCampaign.job_description))
            .order_by(HiringCampaign.created_at.desc())
        )
        result = self.db.execute(stmt)
        return result.scalars().all()

    def get_all_campaigns_for_hiring_manager(self, manager_id: UUID) -> list[HiringCampaign]:
        stmt = (
            select(HiringCampaign)
            .where(
                HiringCampaign.hiring_manager_id == manager_id,
            )
            .options(joinedload(HiringCampaign.job_description))
            .order_by(HiringCampaign.created_at.desc())
        )
        result = self.db.execute(stmt)
        return result.scalars().all()
    
    def get_candidate_count(
        self,
        campaign_id: UUID,
    ) -> int:
        """
        Returns total candidates in a campaign.
        """
        return (
            self.db.query(func.count(CampaignCandidate.id))
            .filter(
                CampaignCandidate.campaign_id == campaign_id,
            )
            .scalar()
            or 0
        )
    
    def get_shortlisted_count(
        self,
        campaign_id: UUID,
    ) -> int:
        """
        Returns total shortlisted candidates in a campaign.
        """
        return (
            self.db.query(func.count(CampaignCandidate.id))
            .filter(
                CampaignCandidate.campaign_id == campaign_id,
                CampaignCandidate.pipeline_stage == PipelineStage.SHORTLISTED,
            )
            .scalar()
            or 0
        )

    def update(self, campaign: HiringCampaign) -> HiringCampaign:
        """Update an existing campaign and refresh it."""
        self.db.flush()
        self.db.refresh(campaign)
        return campaign

    def commit(self) -> None:
        self.db.commit()

    def rollback(self) -> None:
        self.db.rollback()


    def get_expired_campaigns(self) -> list[HiringCampaign]:
        """
        Returns all ACTIVE campaigns whose deadline has passed.
        """
        return (
            self.db.query(HiringCampaign)
            .filter(
                HiringCampaign.status == CampaignStatus.ACTIVE,
                HiringCampaign.deadline.isnot(None),
                HiringCampaign.deadline < datetime.now(timezone.utc),
            )
            .all()
        )
    
    def close_campaign(self, campaign: HiringCampaign) -> HiringCampaign:
        campaign.status = CampaignStatus.CLOSED
        campaign.updated_at = datetime.now(timezone.utc)

        self.db.flush()
        self.db.refresh(campaign)

        return campaign
    
    def search_campaigns(
        self,
        filters: CampaignFilterRequest,
    ) -> list[HiringCampaign]:

        stmt = (
            select(HiringCampaign)
            .options(
                joinedload(HiringCampaign.job_description),
            )
        )

        # Hide closed campaigns by default
        if not filters.show_closed:
            stmt = stmt.where(
                HiringCampaign.status != CampaignStatus.CLOSED
            )

        # Search by campaign name
        if filters.search:
            stmt = stmt.where(
                HiringCampaign.name.ilike(f"%{filters.search}%")
            )

        # Filter by status
        if filters.status:
            stmt = stmt.where(
                HiringCampaign.status == filters.status
            )

        # Filter by Hiring Manager
        if filters.hiring_manager_id:
            stmt = stmt.where(
                HiringCampaign.hiring_manager_id
                == filters.hiring_manager_id
            )

        # Filter by JD
        if filters.jd_id:
            stmt = stmt.where(
                HiringCampaign.jd_id == filters.jd_id
            )

        # Filter by deadline
        if filters.has_deadline is True:
            stmt = stmt.where(
                HiringCampaign.deadline.is_not(None)
            )

        elif filters.has_deadline is False:
            stmt = stmt.where(
                HiringCampaign.deadline.is_(None)
            )

        stmt = stmt.order_by(
            HiringCampaign.created_at.desc()
        )

        result = self.db.execute(stmt)

        return result.scalars().all()
    
    def is_deadline_soon(
        self,
        campaign: HiringCampaign,
        warning_days: int = 3,
    ) -> bool:

        if campaign.deadline is None:
            return False

        now = datetime.now(timezone.utc)

        return now <= campaign.deadline <= now + timedelta(days=warning_days)
    
    def update_scoring_configuration(
        self,
        campaign: HiringCampaign,
        request: CampaignScoringUpdateRequest,
    ) -> HiringCampaign:

        campaign.weight_deterministic = request.weight_deterministic
        campaign.weight_semantic = request.weight_semantic
        campaign.weight_ai = request.weight_ai

        campaign.semantic_threshold = request.semantic_threshold
        campaign.ai_threshold = request.ai_threshold

        campaign.updated_at = datetime.now(timezone.utc)

        self.db.flush()
        self.db.refresh(campaign)

        return campaign

    def get_mandatory_skill_count(self, jd_id) -> int:
        return (
            self.db.query(JDSkill)
            .filter(JDSkill.jd_id == jd_id, JDSkill.mandatory == True)
            .count()
        )
    
    def get_candidate_count(self,campaign_id) -> int:
        return (
            self.db.query(CampaignCandidate)
            .filter(CampaignCandidate.campaign_id == campaign_id)
            .count()
        )
    def get_user(self, user_id: str) -> User | None:
        return self.db.get(User, user_id)

    def get_stage_counts(self, campaign_id) -> dict[str, int]:
        rows = (
            self.db.query(CampaignCandidate.pipeline_stage, func.count())
            .filter(CampaignCandidate.campaign_id == campaign_id)
            .group_by(CampaignCandidate.pipeline_stage)
            .all()
        )
        return {stage.value: count for stage, count in rows}

    def get_audit_entries(self, campaign_id) -> list[AuditLog]:
        return (
            self.db.query(AuditLog)
            .filter(AuditLog.campaign_id == campaign_id)
            .order_by(AuditLog.created_at.desc())
            .all()
        )

    def get_stage_history(self, campaign_id) -> list[CampaignCandidateStageHistory]:
        return (
            self.db.query(CampaignCandidateStageHistory)
            .join(
                CampaignCandidate,
                CampaignCandidateStageHistory.campaign_candidate_id == CampaignCandidate.id,
            )
            .filter(CampaignCandidate.campaign_id == campaign_id)
            .order_by(CampaignCandidateStageHistory.changed_at.desc())
            .all()
        )

    def get_bulk_upload_events(self, campaign_id) -> list[BulkUploadJob]:
        return (
            self.db.query(BulkUploadJob)
            .filter(BulkUploadJob.campaign_id == campaign_id)
            .order_by(BulkUploadJob.created_at.desc())
            .all()
        )

    def update_campaign_status(self, campaign_status: CampaignStatus, campaign_id: UUID) -> HiringCampaign:
        campaign = self.db.query(HiringCampaign).filter(HiringCampaign.id == campaign_id).first()
        if not campaign:
            return None
        campaign.status = campaign_status
        self.db.commit()
        self.db.refresh(campaign)
        return campaign

    # ── S01 Pause an Active Campaign ────────────────────────────────────────

    def count_active_queue_tasks(self, campaign_id: UUID) -> int:
        """
        T01 impact summary: resumes currently in the Celery processing queue for
        this campaign — celery_task_log with status IN (QUEUED, RUNNING) linked
        via campaign_candidate_id.
        """
        return (
            self.db.query(func.count(CeleryTaskLog.id))
            .join(
                CampaignCandidate,
                CeleryTaskLog.campaign_candidate_id == CampaignCandidate.id,
            )
            .filter(
                CampaignCandidate.campaign_id == campaign_id,
                CeleryTaskLog.status.in_([TaskStatus.QUEUED, TaskStatus.RUNNING]),
            )
            .scalar()
            or 0
        )

    def count_processing_bulk_jobs(self, campaign_id: UUID) -> int:
        """T01/T03: bulk_upload_jobs in PROCESSING state for this campaign."""
        return (
            self.db.query(func.count(BulkUploadJob.id))
            .filter(
                BulkUploadJob.campaign_id == campaign_id,
                BulkUploadJob.status == BulkUploadStatus.PROCESSING,
            )
            .scalar()
            or 0
        )

    def suspend_queued_tasks(self, campaign_id: UUID) -> int:
        """
        T02: soft-cancel QUEUED Celery tasks for this campaign by flipping them
        to PAUSED. RUNNING tasks are intentionally left untouched so they finish
        naturally. Returns the number of tasks suspended.

        Bulk UPDATE (no row hydration) — the subquery scopes to this campaign's
        candidates.
        """
        candidate_ids = (
            select(CampaignCandidate.id)
            .where(CampaignCandidate.campaign_id == campaign_id)
            .scalar_subquery()
        )
        result = self.db.execute(
            update(CeleryTaskLog)
            .where(
                CeleryTaskLog.campaign_candidate_id.in_(candidate_ids),
                CeleryTaskLog.status == TaskStatus.QUEUED,
            )
            .values(status=TaskStatus.PAUSED)
            .execution_options(synchronize_session=False)
        )
        return result.rowcount or 0