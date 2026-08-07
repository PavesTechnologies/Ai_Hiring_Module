from uuid import UUID

from sqlalchemy import and_, func, or_, select, text, update
from sqlalchemy.orm import Session, joinedload, lazyload
from app.models.pipeline import PipelineStage
from datetime import datetime, timezone, timedelta

from app.schemas.campaign.campaign_filter_schema import CampaignFilterRequest
from app.schemas.campaign.campaign_schema import CampaignScoringUpdateRequest

from app.models.campaigns import CampaignStatus, HiringCampaign
from app.models.compliance import AuditLog
from app.models.skills import JDSkill, JDSkillVerificationStatus, JDUnknownSkill, JDUnknownSkillStatus
from app.models.pipeline import (CampaignCandidate,
    CampaignCandidateStageHistory,
    DecisionSource,
    DecisionType,
    TransitionSource,
)
from app.models.async_tasks import BulkUploadJob, BulkUploadStatus, CeleryTaskLog, TaskStatus, DeadLetterQueue
from app.models.candidates import Resume, ParseStatus
from app.models.identity import User

class CampaignRepository:

    def __init__(self, db: Session):
        self.db = db

    def create_campaign(self,
        campaign: HiringCampaign,
    ) -> HiringCampaign:
        self.db.add(campaign)
        self.db.flush()
        self.db.refresh(campaign)
        return campaign

    def get_by_id(self,
        campaign_id: UUID,
    ) -> HiringCampaign | None:
        return (self.db.query(HiringCampaign)
            .filter(HiringCampaign.id == campaign_id)
            .first()
        )

    def get_by_id_for_update(self,
        campaign_id: UUID,
    ) -> HiringCampaign | None:
        """
        Locking read (SELECT ... FOR UPDATE) so concurrent candidate-cap
        checks against this campaign are serialized instead of racing —
        the lock is released when the caller commits/rolls back.

        HiringCampaign.job_description defaults to lazy="joined", which
        would otherwise fold a LEFT OUTER JOIN into this query — Postgres
        rejects FOR UPDATE against the nullable side of an outer join
        (FeatureNotSupported: "FOR UPDATE cannot be applied to the nullable
        side of an outer join"). This query never reads job_description, so
        the eager load is overridden off here rather than changed on the
        relationship itself, leaving every other caller unaffected.
        """
        return (self.db.query(HiringCampaign)
            .options(lazyload(HiringCampaign.job_description))
            .filter(HiringCampaign.id == campaign_id)
            .with_for_update()
            .first()
        )

    def get_scoring_configuration(self,
        campaign_id: UUID,
    ) -> HiringCampaign | None:
        """
        Fetch campaign scoring configuration.
        """
        return (self.db.query(HiringCampaign)
            .filter(HiringCampaign.id == campaign_id)
            .first()
        )
    def get_by_name(self,
        org_id: UUID,
        name: str,
    ) -> HiringCampaign | None:
        return (self.db.query(HiringCampaign)
            .filter(HiringCampaign.org_id == org_id,
                func.lower(HiringCampaign.name) == name.lower(),
            )
            .first()
        )

    def get_all_by_org(self,
        org_id: UUID,
    ) -> list[HiringCampaign]:
        return (self.db.query(HiringCampaign)
            .filter(HiringCampaign.org_id == org_id)
            .all()
        )
    
    def get_all_campaigns(self, show_closed: bool = False) -> list[HiringCampaign]:
        stmt = (select(HiringCampaign)
            # .where(#     HiringCampaign.status == "ACTIVE",
            # )
            .options(joinedload(HiringCampaign.job_description))
            .order_by(HiringCampaign.created_at.desc())
        )
        if not show_closed:
            stmt = stmt.where(HiringCampaign.status != CampaignStatus.CLOSED
            )
        result = self.db.execute(stmt)
        return result.scalars().all()

    def get_active_campaigns_minimal(self):
        """
        id + name only, for dropdowns/pickers — a column projection instead
        of loading full HiringCampaign rows (no job_description join needed).
        """
        stmt = (select(HiringCampaign.id, HiringCampaign.name)
            .where(HiringCampaign.status == CampaignStatus.ACTIVE)
            .order_by(HiringCampaign.name)
        )
        return self.db.execute(stmt).all()

    def get_campaigns_by_created_by(
        self,
        created_by: str,
        show_closed: bool = False,
        search: str | None = None,
        status: CampaignStatus | None = None,
        page: int = 1,
        page_size: int = 6,
    ) -> tuple[list[HiringCampaign], int]:
        conditions = [HiringCampaign.created_by == created_by]
        if not show_closed:
            conditions.append(HiringCampaign.status != CampaignStatus.CLOSED)
        if search:
            conditions.append(HiringCampaign.name.ilike(f"%{search}%"))
        if status:
            conditions.append(HiringCampaign.status == status)

        total = self.db.execute(
            select(func.count()).select_from(HiringCampaign).where(*conditions)
        ).scalar_one()

        stmt = (
            select(HiringCampaign)
            .where(*conditions)
            .options(joinedload(HiringCampaign.job_description))
            .order_by(HiringCampaign.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        campaigns = self.db.execute(stmt).scalars().all()
        return campaigns, total

    def get_all_campaigns_for_hiring_manager(self, manager_id: UUID, show_closed: bool = False) -> list[HiringCampaign]:
        stmt = (select(HiringCampaign)
            .where(HiringCampaign.hiring_manager_id == manager_id,
            )
            .options(joinedload(HiringCampaign.job_description))
            .order_by(HiringCampaign.created_at.desc())
        )
        if not show_closed:
            stmt = stmt.where(HiringCampaign.status != CampaignStatus.CLOSED
            )
        result = self.db.execute(stmt)
        return result.scalars().all()

    def get_hiring_manager_names(self, hiring_manager_ids: list[str]) -> dict[str, str]:
        """
        Batch-resolves hiring_manager_id -> full_name for list endpoints, avoiding
        an N+1 query per campaign row (same User model get_campaign_by_id already
        uses for the single-campaign case).
        """
        ids = [hm_id for hm_id in set(hiring_manager_ids) if hm_id]
        if not ids:
            return {}

        stmt = select(User.id, User.full_name).where(User.id.in_(ids))
        result = self.db.execute(stmt)
        return {row.id: row.full_name for row in result}
    
    def get_candidate_count(self,
        campaign_id: UUID,
    ) -> int:
        """
        Returns total candidates in a campaign.
        """
        return (self.db.query(func.count(CampaignCandidate.id))
            .filter(CampaignCandidate.campaign_id == campaign_id,
            )
            .scalar()
            or 0
        )
    
    def get_shortlisted_count(self,
        campaign_id: UUID,
    ) -> int:
        """
        Returns total shortlisted candidates in a campaign.
        """
        return (self.db.query(func.count(CampaignCandidate.id))
            .filter(CampaignCandidate.campaign_id == campaign_id,
                CampaignCandidate.pipeline_stage == PipelineStage.SHORTLISTED,
            )
            .scalar()
            or 0
        )

    def get_selected_count(self,
        campaign_id: UUID,
    ) -> int:
        """
        Positions filled — the count max_candidates is measured against.

        max_candidates is the number of openings, not an intake limit, so a
        slot is consumed when a candidate reaches SELECTED and not when a
        resume is uploaded. SELECTED is terminal (no transition out of it is
        seeded in allowed_transitions), so a plain equality count cannot
        under-count the way a mid-pipeline stage would.
        """
        return (self.db.query(func.count(CampaignCandidate.id))
            .filter(CampaignCandidate.campaign_id == campaign_id,
                CampaignCandidate.pipeline_stage == PipelineStage.SELECTED,
            )
            .scalar()
            or 0
        )

    def get_hm_review_count(self,
        campaign_id: UUID,
    ) -> int:
        """
        candidates currently awaiting hiring-manager review in this
        campaign — used to warn HR_ADMIN that pending decisions may need
        re-communicating to a newly-reassigned hiring manager.
        """
        return (self.db.query(func.count(CampaignCandidate.id))
            .filter(CampaignCandidate.campaign_id == campaign_id,
                CampaignCandidate.pipeline_stage == PipelineStage.HM_REVIEW,
            )
            .scalar()
            or 0
        )

    def get_overdue_review_count(self, campaign_id: UUID, sla_days: int) -> int:
        """
        candidates currently in HM_REVIEW whose most recent transition
        into that stage is older than sla_days, with no decision since.
        """
        cutoff = datetime.now(timezone.utc) - timedelta(days=sla_days)

        latest_entry = (select(CampaignCandidateStageHistory.campaign_candidate_id,
                func.max(CampaignCandidateStageHistory.changed_at).label("entered_at"),
            )
            .where(CampaignCandidateStageHistory.to_stage == PipelineStage.HM_REVIEW)
            .group_by(CampaignCandidateStageHistory.campaign_candidate_id)
            .subquery()
        )

        stmt = (select(func.count(CampaignCandidate.id))
            .join(latest_entry, latest_entry.c.campaign_candidate_id == CampaignCandidate.id)
            .where(CampaignCandidate.campaign_id == campaign_id,
                CampaignCandidate.pipeline_stage == PipelineStage.HM_REVIEW,
                latest_entry.c.entered_at <= cutoff,
            )
        )
        return self.db.execute(stmt).scalar() or 0

    def is_pipeline_stalled(self, campaign_id: UUID, stale_days: int) -> bool:
        """
        True if the campaign has candidates but none have been added
        in the last stale_days days.
        """
        latest_added_at = (self.db.query(func.max(CampaignCandidate.created_at))
            .filter(CampaignCandidate.campaign_id == campaign_id)
            .scalar()
        )
        if latest_added_at is None:
            return False

        cutoff = datetime.now(timezone.utc) - timedelta(days=stale_days)
        return latest_added_at <= cutoff

    def update(self, campaign: HiringCampaign) -> HiringCampaign:
        """Update an existing campaign and refresh it."""
        self.db.flush()
        self.db.refresh(campaign)
        return campaign

    def commit(self) -> None:
        self.db.commit()

    def rollback(self) -> None:
        self.db.rollback()


    def get_expired_campaigns(self, limit: int | None = None) -> list[HiringCampaign]:
        """
        Returns ACTIVE campaigns whose deadline has passed. an
        optional limit lets the caller process expired campaigns in batches
        instead of locking every expired row in one huge transaction.
        """
        stmt = (self.db.query(HiringCampaign)
            .filter(HiringCampaign.status == CampaignStatus.ACTIVE,
                HiringCampaign.deadline.isnot(None),
                HiringCampaign.deadline < datetime.now(timezone.utc),
            )
        )
        if limit is not None:
            stmt = stmt.limit(limit)
        return stmt.all()
    
    def close_campaign(self, campaign: HiringCampaign) -> HiringCampaign:
        campaign.status = CampaignStatus.CLOSED
        campaign.updated_at = datetime.now(timezone.utc)

        self.db.flush()
        self.db.refresh(campaign)

        return campaign
    
    def search_campaigns(self,
        filters: CampaignFilterRequest,
    ) -> list[HiringCampaign]:

        stmt = (select(HiringCampaign)
            .options(joinedload(HiringCampaign.job_description),
            )
        )

        # Hide closed campaigns by default
        if not filters.show_closed:
            stmt = stmt.where(HiringCampaign.status != CampaignStatus.CLOSED
            )

        # Search by campaign name
        if filters.search:
            stmt = stmt.where(HiringCampaign.name.ilike(f"%{filters.search}%")
            )

        # Filter by status
        if filters.status:
            stmt = stmt.where(HiringCampaign.status == filters.status
            )

        # Filter by Hiring Manager
        if filters.hiring_manager_id:
            stmt = stmt.where(HiringCampaign.hiring_manager_id
                == filters.hiring_manager_id
            )

        # Filter by JD
        if filters.jd_id:
            stmt = stmt.where(HiringCampaign.jd_id == filters.jd_id
            )

        # Filter by deadline
        if filters.has_deadline is True:
            stmt = stmt.where(HiringCampaign.deadline.is_not(None)
            )

        elif filters.has_deadline is False:
            stmt = stmt.where(HiringCampaign.deadline.is_(None)
            )

        stmt = stmt.order_by(HiringCampaign.created_at.desc()
        )

        result = self.db.execute(stmt)

        return result.scalars().all()
    
    def is_deadline_soon(self,
        campaign: HiringCampaign,
        warning_days: int = 3,
    ) -> bool:

        if campaign.deadline is None:
            return False

        now = datetime.now(timezone.utc)

        return now <= campaign.deadline <= now + timedelta(days=warning_days)
    
    def update_scoring_configuration(self,
        campaign: HiringCampaign,
        request: CampaignScoringUpdateRequest,
    ) -> HiringCampaign:

        campaign.weight_deterministic = request.weight_deterministic
        campaign.weight_semantic = request.weight_semantic
        campaign.weight_ai = request.weight_ai

        campaign.semantic_threshold = request.semantic_threshold
        campaign.ai_threshold = request.ai_threshold
        campaign.deterministic_threshold = request.deterministic_threshold

        campaign.updated_at = datetime.now(timezone.utc)

        self.db.flush()
        self.db.refresh(campaign)

        return campaign

    def get_mandatory_skill_count(self, jd_id) -> int:
        return (self.db.query(JDSkill)
            .filter(JDSkill.jd_id == jd_id, JDSkill.mandatory == True)
            .count()
        )

    def get_mandatory_unverified_skill_count(self, jd_id) -> int:
        """mandatory jd_skills not yet AUTO_VERIFIED (still PENDING_REVIEW)."""
        return (self.db.query(JDSkill)
            .filter(JDSkill.jd_id == jd_id,
                JDSkill.mandatory == True,
                JDSkill.verification_status == JDSkillVerificationStatus.PENDING_REVIEW,
            )
            .count()
        )

    def get_unresolved_unknown_skill_count(self, jd_id) -> int:
        """
        "no blocking parse failures" — the closest concrete, queryable
        signal is unknown skills the extraction pipeline couldn't confidently
        match to skill_ontology, still sitting unresolved for this JD.
        """
        return (self.db.query(JDUnknownSkill)
            .filter(JDUnknownSkill.jd_id == jd_id,
                JDUnknownSkill.status == JDUnknownSkillStatus.PENDING,
            )
            .count()
        )
    
    def get_user(self, user_id: str) -> User | None:
        return self.db.get(User, user_id)

    def get_user_names(self, user_ids: list[str]) -> dict[str, str]:
        """Batch id → full_name lookup so callers rendering many rows
        (e.g. the timeline) don't issue one query per actor."""
        if not user_ids:
            return {}
        rows = (self.db.query(User.id, User.full_name)
            .filter(User.id.in_(user_ids))
            .all()
        )
        return {str(user_id): full_name for user_id, full_name in rows}

    def get_stage_counts(self, campaign_id) -> dict[str, int]:
        rows = (self.db.query(CampaignCandidate.pipeline_stage, func.count())
            .filter(CampaignCandidate.campaign_id == campaign_id)
            .group_by(CampaignCandidate.pipeline_stage)
            .all()
        )
        return {stage.value: count for stage, count in rows}

    def get_audit_entries(self, campaign_id) -> list[AuditLog]:
        return (self.db.query(AuditLog)
            .filter(AuditLog.campaign_id == campaign_id)
            .order_by(AuditLog.created_at.desc())
            .all()
        )

    def get_stage_history(self, campaign_id) -> list[CampaignCandidateStageHistory]:
        return (self.db.query(CampaignCandidateStageHistory)
            .join(CampaignCandidate,
                CampaignCandidateStageHistory.campaign_candidate_id == CampaignCandidate.id,
            )
            .filter(CampaignCandidate.campaign_id == campaign_id)
            .order_by(CampaignCandidateStageHistory.changed_at.desc())
            .all()
        )

    def get_bulk_upload_events(self, campaign_id) -> list[BulkUploadJob]:
        return (self.db.query(BulkUploadJob)
            .filter(BulkUploadJob.campaign_id == campaign_id)
            .order_by(BulkUploadJob.created_at.desc())
            .all()
        )

    # ── S01 Pause an Active Campaign ────────────────────────────────────────

    def count_active_queue_tasks(self, campaign_id: UUID) -> int:
        """
        T01 impact summary: resumes currently in the Celery processing queue for
        this campaign — celery_task_log with status IN (QUEUED, RUNNING) linked
        via campaign_candidate_id.
        """
        return (self.db.query(func.count(CeleryTaskLog.id))
            .join(CampaignCandidate,
                CeleryTaskLog.campaign_candidate_id == CampaignCandidate.id,
            )
            .filter(CampaignCandidate.campaign_id == campaign_id,
                CeleryTaskLog.status.in_([TaskStatus.QUEUED, TaskStatus.RUNNING]),
            )
            .scalar()
            or 0
        )

    def count_processing_bulk_jobs(self, campaign_id: UUID) -> int:
        """T01/T03: bulk_upload_jobs in PROCESSING state for this campaign."""
        return (self.db.query(func.count(BulkUploadJob.id))
            .filter(BulkUploadJob.campaign_id == campaign_id,
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
        candidate_ids = (select(CampaignCandidate.id)
            .where(CampaignCandidate.campaign_id == campaign_id)
            .scalar_subquery()
        )
        result = self.db.execute(update(CeleryTaskLog)
            .where(CeleryTaskLog.campaign_candidate_id.in_(candidate_ids),
                CeleryTaskLog.status == TaskStatus.QUEUED,
            )
            .values(status=TaskStatus.PAUSED)
            .execution_options(synchronize_session=False)
        )
        return result.rowcount or 0

    # ── S02 Resume a Paused Campaign ────────────────────────────────────────

    def count_paused_tasks(self, campaign_id: UUID) -> int:
        """T01: Celery tasks suspended during the pause (status = PAUSED)."""
        return (self.db.query(func.count(CeleryTaskLog.id))
            .join(CampaignCandidate,
                CeleryTaskLog.campaign_candidate_id == CampaignCandidate.id,
            )
            .filter(CampaignCandidate.campaign_id == campaign_id,
                CeleryTaskLog.status == TaskStatus.PAUSED,
            )
            .scalar()
            or 0
        )

    def count_pending_resumes(self, campaign_id: UUID) -> int:
        """
        T01: resumes uploaded but not yet queued for processing — parse_status =
        PENDING, linked to this campaign via campaign_candidates.
        """
        return (self.db.query(func.count(func.distinct(Resume.id)))
            .join(CampaignCandidate, CampaignCandidate.resume_id == Resume.id)
            .filter(CampaignCandidate.campaign_id == campaign_id,
                Resume.parse_status == ParseStatus.PENDING,
            )
            .scalar()
            or 0
        )

    def get_paused_tasks(self, campaign_id: UUID) -> list[CeleryTaskLog]:
        """
        T02: hydrated PAUSED CeleryTaskLog rows for this campaign — unlike a
        bulk UPDATE, these need to be loaded so each one can actually be
        resubmitted to the Celery broker before its status flips to QUEUED.
        """
        return (self.db.query(CeleryTaskLog)
            .join(CampaignCandidate,
                CeleryTaskLog.campaign_candidate_id == CampaignCandidate.id,
            )
            .filter(CampaignCandidate.campaign_id == campaign_id,
                CeleryTaskLog.status == TaskStatus.PAUSED,
            )
            .all()
        )

    def set_resume_task_id(self, resume: Resume, task_id: str) -> Resume:
        """Mirrors ResumeRepository.set_task_id — persists the processing task's id at enqueue time."""
        resume.task_id = task_id
        self.db.flush()
        return resume

    def get_pending_resumes(self, campaign_id: UUID) -> list[Resume]:
        """
        T02: hydrated Resume rows with parse_status = PENDING for this
        campaign — these need a new RESUME_PARSE task actually submitted on
        resume, not just counted.
        """
        return (self.db.query(Resume)
            .join(CampaignCandidate, CampaignCandidate.resume_id == Resume.id)
            .filter(CampaignCandidate.campaign_id == campaign_id,
                Resume.parse_status == ParseStatus.PENDING,
            )
            .distinct()
            .all()
        )

    # ── S03 Close a Campaign Manually ───────────────────────────────────────

    def count_pending_human_decision(self, campaign_id: UUID) -> int:
        """T01: candidates in INTERVIEW or HM_REVIEW — need a human decision before closing."""
        return (self.db.query(func.count(CampaignCandidate.id))
            .filter(CampaignCandidate.campaign_id == campaign_id,
                CampaignCandidate.pipeline_stage.in_([PipelineStage.INTERVIEW, PipelineStage.HM_REVIEW]
                ),
            )
            .scalar()
            or 0
        )

    def kill_queued_tasks(self, campaign_id: UUID) -> int:
        """
        T02: closure is terminal (unlike pause) — QUEUED tasks are killed to
        DEAD, not soft-cancelled to PAUSED, since there's no resume to
        re-queue them later. RUNNING tasks are left to finish naturally.
        """
        candidate_ids = (select(CampaignCandidate.id)
            .where(CampaignCandidate.campaign_id == campaign_id)
            .scalar_subquery()
        )
        result = self.db.execute(update(CeleryTaskLog)
            .where(CeleryTaskLog.campaign_candidate_id.in_(candidate_ids),
                CeleryTaskLog.status == TaskStatus.QUEUED,
            )
            .values(status=TaskStatus.DEAD,
                error_message="Campaign closed by HR_ADMIN before task could execute.",
            )
            .execution_options(synchronize_session=False)
        )
        return result.rowcount or 0

    def cancel_pending_bulk_jobs(self, campaign_id: UUID) -> int:
        """T02: bulk_upload_jobs still PENDING/EXTRACTING are cancelled outright on closure."""
        result = self.db.execute(update(BulkUploadJob)
            .where(BulkUploadJob.campaign_id == campaign_id,
                BulkUploadJob.status.in_([BulkUploadStatus.PENDING, BulkUploadStatus.EXTRACTING]),
            )
            .values(status=BulkUploadStatus.FAILED,
                error_summary="Campaign closed during upload.",
            )
            .execution_options(synchronize_session=False)
        )
        return result.rowcount or 0

    def mark_processing_bulk_jobs_partial_failure(self, campaign_id: UUID) -> int:
        """
        E03-when a campaign auto-closes because its candidate cap was
        just reached, any bulk_upload_jobs still PROCESSING get marked
        PARTIAL_FAILURE (some files were processed before the cap hit, not a
        clean FAILED) rather than left to run against a now-closed campaign.
        """
        result = self.db.execute(update(BulkUploadJob)
            .where(BulkUploadJob.campaign_id == campaign_id,
                BulkUploadJob.status == BulkUploadStatus.PROCESSING,
            )
            .values(status=BulkUploadStatus.PARTIAL_FAILURE,
                error_summary="Campaign reached maximum candidate limit during upload.",
            )
            .execution_options(synchronize_session=False)
        )
        return result.rowcount or 0

    # Monitor Campaign Pipeline Health ────────────────────────────

    def get_task_status_counts(self, campaign_id: UUID) -> dict[str, int]:
        """T02: celery_task_log status breakdown for this campaign's tasks."""
        rows = (self.db.query(CeleryTaskLog.status, func.count())
            .join(CampaignCandidate,
                CeleryTaskLog.campaign_candidate_id == CampaignCandidate.id,
            )
            .filter(CampaignCandidate.campaign_id == campaign_id)
            .group_by(CeleryTaskLog.status)
            .all()
        )
        return {status.value: count for status, count in rows}

    def get_dead_letter_queue_entries(self, campaign_id: UUID) -> list[DeadLetterQueue]:
        """
        (widened from the candidate-only join): campaign-linked
        DLQ rows via campaign_candidate_id OR resume_id. Tasks that died before
        their CampaignCandidate row existed carry only resume_id, and the old
        candidate-only join silently hid them from the campaign view.
        """
        candidate_ids = select(CampaignCandidate.id).where(CampaignCandidate.campaign_id == campaign_id
        )
        resume_ids = select(CampaignCandidate.resume_id).where(CampaignCandidate.campaign_id == campaign_id
        )
        return (self.db.query(DeadLetterQueue)
            .filter(or_(DeadLetterQueue.campaign_candidate_id.in_(candidate_ids),
                    DeadLetterQueue.resume_id.in_(resume_ids),
                )
            )
            .order_by(DeadLetterQueue.moved_to_dlq_at.desc())
            .all()
        )

    def get_pending_resume_counts_by_campaign(self) -> list[tuple[UUID, str, int]]:
        """
        Epic 4 (M05-E04) Phase D12 - platform-wide, grouped by campaign,
        for the upload-queue dashboard's per-campaign breakdown. Joined
        to HiringCampaign for the display name up front, avoiding a
        second name-resolution query for the (small, capped) set of
        campaigns that end up in the final breakdown. Not filtered to
        ACTIVE-only campaigns - a PAUSED campaign can still have real
        lingering PENDING resumes worth surfacing here.
        """
        rows = (
            self.db.query(HiringCampaign.id, HiringCampaign.name, func.count())
            .join(CampaignCandidate, CampaignCandidate.campaign_id == HiringCampaign.id)
            .join(Resume, Resume.id == CampaignCandidate.resume_id)
            .filter(Resume.parse_status == ParseStatus.PENDING)
            .group_by(HiringCampaign.id, HiringCampaign.name)
            .all()
        )
        return [(campaign_id, name, count) for campaign_id, name, count in rows]

    def get_queued_resume_task_counts_by_campaign(self, task_type: str) -> list[tuple[UUID, str, int]]:
        """
        Epic 4 (M05-E04) Phase D12 - platform-wide, grouped by campaign.
        task_type is passed in (not hardcoded here) so this stays in
        sync with whichever literal the caller's own platform-wide
        metric uses, rather than duplicating that string in two places.
        """
        rows = (
            self.db.query(HiringCampaign.id, HiringCampaign.name, func.count())
            .join(CampaignCandidate, CampaignCandidate.campaign_id == HiringCampaign.id)
            .join(CeleryTaskLog, CeleryTaskLog.campaign_candidate_id == CampaignCandidate.id)
            .filter(
                CeleryTaskLog.task_type == task_type,
                CeleryTaskLog.status == TaskStatus.QUEUED,
            )
            .group_by(HiringCampaign.id, HiringCampaign.name)
            .all()
        )
        return [(campaign_id, name, count) for campaign_id, name, count in rows]

    def get_task_type_breakdown(self, campaign_id: UUID) -> list[dict]:
        """
        per-task_type status counts + avg duration + token usage for
        this campaign's tasks. Grouped by the ACTUAL task_type strings in
        celery_task_log (the spec's RESUME_PARSE/EMBED_RESUME/... taxonomy
        doesn't exist — embedding/normalization run as stages inside the parse
        task, not as separate Celery tasks). avg_duration_ms is derived from
        completed_at - started_at since no duration column exists.
        """
        rows = (self.db.query(CeleryTaskLog.task_type,
                CeleryTaskLog.status,
                func.count(CeleryTaskLog.id),
                func.avg(func.extract("epoch", CeleryTaskLog.completed_at)
                    - func.extract("epoch", CeleryTaskLog.started_at)
                ),
                func.coalesce(func.sum(CeleryTaskLog.token_count), 0),
            )
            .join(CampaignCandidate,
                CeleryTaskLog.campaign_candidate_id == CampaignCandidate.id,
            )
            .filter(CampaignCandidate.campaign_id == campaign_id)
            .group_by(CeleryTaskLog.task_type, CeleryTaskLog.status)
            .all()
        )

        by_type: dict[str, dict] = {}
        for task_type, task_status, count, avg_seconds, tokens in rows:
            entry = by_type.setdefault(task_type,
                {
                    "task_type": task_type,
                    "status_counts": {},
                    "avg_duration_ms": None,
                    "total_token_count": 0,
                },
            )
            entry["status_counts"][task_status.value] = count
            entry["total_token_count"] += int(tokens or 0)
            # duration only means anything for rows that actually completed
            if task_status == TaskStatus.SUCCESS and avg_seconds is not None:
                entry["avg_duration_ms"] = round(float(avg_seconds) * 1000, 1)
        return list(by_type.values())

    def get_dlq_entries_by_ids(self, campaign_id: UUID, dlq_ids: list[UUID]
    ) -> list[DeadLetterQueue]:
        """the selected entries, re-validated as belonging to this campaign."""
        wanted = set(dlq_ids)
        return [
            e for e in self.get_dead_letter_queue_entries(campaign_id)
            if e.id in wanted
        ]

    def count_dlq_chain(self, entry: DeadLetterQueue) -> int:
        """
        replay-limit guard: how many times has this same piece of work
        already dead-lettered? Every failed replay produces a NEW dlq row for
        the same (task_type, entity), so the chain length IS the attempt count.
        """
        conditions = [DeadLetterQueue.task_type == entry.task_type]
        if entry.campaign_candidate_id is not None:
            conditions.append(DeadLetterQueue.campaign_candidate_id == entry.campaign_candidate_id
            )
        elif entry.resume_id is not None:
            conditions.append(DeadLetterQueue.resume_id == entry.resume_id)
        else:
            return 1  # unlinkable entry — no chain to count
        return (self.db.query(func.count(DeadLetterQueue.id)).filter(*conditions).scalar()
            or 1
        )

    # ── Stalled candidates ─────────────────────────────────────────

    def get_stalled_candidates(self,
        campaign_id: UUID,
        screening_sla_hours: float,
        hm_review_sla_days: float,
        interview_sla_days: float,
    ) -> list[dict]:
        """
        campaign_candidates sitting in the same stage past their
        per-stage SLA (updated_at is the last-touched marker per spec).
        Computed live — no stalled_candidates table exists; the "view" is
        this query. Returns dicts with stall context; last_action_by is the
        actor of the most recent audit_log entry for that candidate entity.
        """
        now = datetime.now(timezone.utc)
        cutoffs = {
            PipelineStage.SCREENING: now - timedelta(hours=screening_sla_hours),
            PipelineStage.HM_REVIEW: now - timedelta(days=hm_review_sla_days),
            PipelineStage.INTERVIEW: now - timedelta(days=interview_sla_days),
        }
        rows = (self.db.query(CampaignCandidate)
            .filter(CampaignCandidate.campaign_id == campaign_id,
                or_(*[
                    and_(CampaignCandidate.pipeline_stage == stage,
                        CampaignCandidate.updated_at < cutoff,
                    )
                    for stage, cutoff in cutoffs.items()
                ]),
            )
            .order_by(CampaignCandidate.updated_at.asc())
            .all()
        )
        if not rows:
            return []

        ids = [r.id for r in rows]

        # batch: which stalled SCREENING candidates have a dead task / DLQ row
        failed_ids = {
            cid for (cid,) in self.db.query(CeleryTaskLog.campaign_candidate_id)
            .filter(CeleryTaskLog.campaign_candidate_id.in_(ids),
                CeleryTaskLog.status == TaskStatus.DEAD,
            ).all()
        } | {
            cid for (cid,) in self.db.query(DeadLetterQueue.campaign_candidate_id)
            .filter(DeadLetterQueue.campaign_candidate_id.in_(ids)).all()
        }

        # batch: latest audit actor per candidate entity
        last_actor: dict = {}
        audit_rows = (self.db.query(AuditLog.entity_id, AuditLog.actor_id)
            .filter(AuditLog.entity_id.in_(ids))
            .order_by(AuditLog.created_at.desc())
            .all()
        )
        for entity_id, actor_id in audit_rows:
            last_actor.setdefault(entity_id, actor_id)

        actor_names = self.get_hiring_manager_names([a for a in last_actor.values() if a]
        )

        stall_reasons = {
            PipelineStage.SCREENING: "SCREENING_OVERDUE",
            PipelineStage.HM_REVIEW: "HM_REVIEW_OVERDUE",
            PipelineStage.INTERVIEW: "INTERVIEW_NOT_SCHEDULED",
        }
        result = []
        for cc in rows:
            reason = stall_reasons[cc.pipeline_stage]
            if cc.pipeline_stage == PipelineStage.SCREENING and cc.id in failed_ids:
                reason = "AI_EVALUATION_FAILED"
            actor_id = last_actor.get(cc.id)
            result.append({
                "campaign_candidate_id": cc.id,
                "pipeline_stage": cc.pipeline_stage.value,
                "days_stalled": round((now - cc.updated_at).total_seconds() / 86400, 1),
                "last_updated_at": cc.updated_at,
                "stall_reason": reason,
                "last_action_by": actor_names.get(actor_id) or actor_id,
                "has_dead_letter_tasks": cc.id in failed_ids,
            })
        return result

    def get_campaign_candidate(self, campaign_id: UUID, campaign_candidate_id: UUID) -> CampaignCandidate | None:
        """Scoped fetch — validates the candidate actually belongs to this campaign."""
        return (self.db.query(CampaignCandidate)
            .filter(CampaignCandidate.id == campaign_candidate_id,
                CampaignCandidate.campaign_id == campaign_id,
            )
            .first()
        )

    def transition_candidate_stage(self,
        cc: CampaignCandidate,
        to_stage,
        changed_by: str,
        change_reason: str,
        transition_source: TransitionSource,
        set_fraud_flag: bool = False,
    ) -> None:
        """
        the first real stage-transition writer in this codebase —
        updates pipeline_stage AND records the campaign_candidate_stage_history
        row (previously only the initial UPLOADED insert ever wrote history).
        """
        from_stage = cc.pipeline_stage
        cc.pipeline_stage = to_stage
        if set_fraud_flag:
            cc.is_fraud_flagged = True
        ai_evaluation = cc.ai_evaluation
        self.db.add(CampaignCandidateStageHistory(campaign_candidate_id=cc.id,
            from_stage=from_stage,
            to_stage=to_stage,
            changed_by=changed_by,
            change_reason=change_reason,
            transition_source=transition_source,
            scores_snapshot={
                "composite_score": float(cc.composite_score) if cc.composite_score is not None else None,
                "deterministic_score": float(cc.deterministic_score) if cc.deterministic_score is not None else None,
                "semantic_score": float(cc.semantic_score) if cc.semantic_score is not None else None,
                "ai_ats_score": (
                    float(ai_evaluation.ai_ats_score)
                    if ai_evaluation is not None and ai_evaluation.ai_ats_score is not None else None
                ),
                "decision_type": cc.decision_type.value if cc.decision_type else None,
                "decision_source": cc.decision_source.value if cc.decision_source else None,
                "decision_reason": cc.decision_reason,
                "decision_details": cc.decision_details,
            },
        ))
        self.db.flush()

    def get_unreplayed_dlq_ids_for_candidate(self, campaign_candidate_id: UUID) -> list[UUID]:
        """Re-Process: this candidate's replayable (not-yet-replayed) DLQ entries."""
        return [
            row[0] for row in self.db.query(DeadLetterQueue.id)
            .filter(DeadLetterQueue.campaign_candidate_id == campaign_candidate_id,
                DeadLetterQueue.replayed_at.is_(None),
            ).all()
        ]

    # ── Rejection analytics ────────────────────────────────────────

    # Every rejection-analytics method below reads campaign_candidate_
    # stage_history rows (to_stage='REJECTED') instead of the removed
    # candidate_rejections table - scores_snapshot was enriched with
    # decision_type/decision_source/decision_reason/decision_details at
    # write time (StageTransitionService), preserving full per-event
    # history (a candidate can be rejected, HR-overridden, then rejected
    # again) rather than collapsing to campaign_candidates' latest decision.

    def get_rejection_layer_breakdown(self, campaign_id: UUID) -> dict[str, int]:
        rows = self.db.execute(text("""
            SELECT sh.scores_snapshot->>'decision_source' AS decision_source, COUNT(*) AS cnt
            FROM campaign_candidate_stage_history sh
            JOIN campaign_candidates cc ON cc.id = sh.campaign_candidate_id
            WHERE cc.campaign_id = :campaign_id
              AND sh.to_stage = 'REJECTED'
              AND sh.scores_snapshot->>'decision_source' IS NOT NULL
            GROUP BY decision_source
        """), {"campaign_id": str(campaign_id)}).all()
        return {row.decision_source: row.cnt for row in rows}

    def get_top_rejection_reasons(self, campaign_id: UUID, limit: int = 10) -> list[dict]:
        rows = self.db.execute(text("""
            SELECT sh.scores_snapshot->>'decision_reason' AS reason, COUNT(*) AS cnt
            FROM campaign_candidate_stage_history sh
            JOIN campaign_candidates cc ON cc.id = sh.campaign_candidate_id
            WHERE cc.campaign_id = :campaign_id
              AND sh.to_stage = 'REJECTED'
              AND sh.scores_snapshot->>'decision_reason' IS NOT NULL
            GROUP BY reason
            ORDER BY cnt DESC
            LIMIT :limit
        """), {"campaign_id": str(campaign_id), "limit": limit}).all()

        result = []
        for row in rows:
            sample = self.db.execute(text("""
                SELECT sh.scores_snapshot->'decision_details' AS detail
                FROM campaign_candidate_stage_history sh
                JOIN campaign_candidates cc ON cc.id = sh.campaign_candidate_id
                WHERE cc.campaign_id = :campaign_id
                  AND sh.to_stage = 'REJECTED'
                  AND sh.scores_snapshot->>'decision_reason' = :reason
                  AND sh.scores_snapshot->'decision_details' IS NOT NULL
                LIMIT 1
            """), {"campaign_id": str(campaign_id), "reason": row.reason}).first()
            result.append({
                "reason": row.reason,
                "count": row.cnt,
                "sample_detail": sample.detail if sample else None,
            })
        return result

    def get_missing_mandatory_skill_counts(self, campaign_id: UUID) -> list[tuple[str, int]]:
        """
        per-skill counts from decision_details->'missing_skills' (a JSONB
        array of canonical skill NAMES — the shape the deterministic
        scoring task actually writes). Raw SQL because
        jsonb_array_elements_text has no clean ORM equivalent.
        """
        rows = self.db.execute(text("""
            SELECT skill, COUNT(*) AS cnt
            FROM campaign_candidate_stage_history sh
            JOIN campaign_candidates cc ON cc.id = sh.campaign_candidate_id,
            LATERAL jsonb_array_elements_text(sh.scores_snapshot->'decision_details'->'missing_skills') AS skill
            WHERE cc.campaign_id = :campaign_id
              AND sh.to_stage = 'REJECTED'
              AND sh.scores_snapshot->>'decision_source' = 'DETERMINISTIC'
              AND sh.scores_snapshot->'decision_details' ? 'missing_skills'
            GROUP BY skill
            ORDER BY cnt DESC
        """), {"campaign_id": str(campaign_id)}).all()
        return [(r.skill, r.cnt) for r in rows]

    def get_deterministic_rejection_rate(self, campaign_id: UUID) -> float:
        """
        T03: percentage of this campaign's total candidates rejected at the
        DETERMINISTIC layer. 0.0 for a campaign with no candidates yet
        (nothing to alert on).
        """
        total = self.get_candidate_count(campaign_id)
        if total == 0:
            return 0.0

        rejected = (self.db.query(func.count(CampaignCandidate.id))
            .filter(CampaignCandidate.campaign_id == campaign_id,
                CampaignCandidate.decision_type == DecisionType.REJECTED,
                CampaignCandidate.decision_source == DecisionSource.DETERMINISTIC,
            )
            .scalar()
            or 0
        )
        return (rejected / total) * 100

    def get_deterministic_rejection_details(
        self,
        campaign_id: UUID | None = None,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
    ) -> list[tuple[dict, UUID]]:
        """
        (decision_details, campaign_id) pairs for every DETERMINISTIC
        rejection stage-history transition, optionally scoped to one
        campaign and/or a changed_at date range - campaign_id=None means
        platform-wide, mirroring the old CandidateRejectionRepository.
        get_by_campaign's exact call shape/semantics, now read from
        campaign_candidate_stage_history's scores_snapshot instead of the
        removed candidate_rejections table.
        """
        filters = ["sh.to_stage = 'REJECTED'", "sh.scores_snapshot->>'decision_source' = 'DETERMINISTIC'"]
        params: dict = {}
        if campaign_id is not None:
            filters.append("cc.campaign_id = :campaign_id")
            params["campaign_id"] = str(campaign_id)
        if date_from is not None:
            filters.append("sh.changed_at >= :date_from")
            params["date_from"] = date_from
        if date_to is not None:
            filters.append("sh.changed_at <= :date_to")
            params["date_to"] = date_to

        rows = self.db.execute(text(f"""
            SELECT sh.scores_snapshot->'decision_details' AS detail, cc.campaign_id AS campaign_id
            FROM campaign_candidate_stage_history sh
            JOIN campaign_candidates cc ON cc.id = sh.campaign_candidate_id
            WHERE {' AND '.join(filters)}
        """), params).all()
        return [(row.detail or {}, row.campaign_id) for row in rows]

    def get_average_screening_hours(self, campaign_id: UUID) -> float | None:
        """
        T03: average hours-in-SCREENING for candidates CURRENTLY in that
        stage (i.e. "stuck", not a historical average over completed
        transitions) — measured from each candidate's most recent
        transition into SCREENING to now. None if nobody is in SCREENING.
        """
        latest_entry = (select(CampaignCandidateStageHistory.campaign_candidate_id,
                func.max(CampaignCandidateStageHistory.changed_at).label("entered_at"),
            )
            .where(CampaignCandidateStageHistory.to_stage == PipelineStage.SCREENING)
            .group_by(CampaignCandidateStageHistory.campaign_candidate_id)
            .subquery()
        )

        rows = (self.db.query(latest_entry.c.entered_at)
            .join(CampaignCandidate,
                CampaignCandidate.id == latest_entry.c.campaign_candidate_id,
            )
            .filter(CampaignCandidate.campaign_id == campaign_id,
                CampaignCandidate.pipeline_stage == PipelineStage.SCREENING,
            )
            .all()
        )
        if not rows:
            return None

        now = datetime.now(timezone.utc)
        hours = [(now - r.entered_at).total_seconds() / 3600 for r in rows]
        return sum(hours) / len(hours)