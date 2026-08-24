from datetime import datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy import and_, distinct, func, or_, select
from sqlalchemy.orm import Session

from app.models.async_tasks import BulkUploadJob, BulkUploadStatus
from app.models.campaigns import CampaignStatus, HiringCampaign
from app.models.candidates import Resume
from app.models.config import CircuitBreakerState
from app.models.identity import User
from app.models.jd.job_descriptions import JobDescription
from app.models.pipeline import (
    AIEvaluationStatus,
    CampaignCandidate,
    CampaignCandidateAIEvaluation,
    CampaignCandidateStageHistory,
    PipelineStage,
)
from app.models.skills import UnknownSkill, UnknownSkillStatus

# M11-E01-S01-T02 names these three explicitly.
MONITORED_SERVICES = ("GEMINI_FLASH", "EMBEDDING_SERVICE", "MINIO")


class DashboardRepository:

    def __init__(self, db: Session):
        self.db = db

    # ── shared helpers ────────────────────────────────────────────────

    def _stall_predicate(self, now, screening_sla_hours, hm_review_sla_days, interview_sla_days):
        """
        Deliberately identical to CampaignRepository.get_stalled_candidate_rows -
        same per-stage cutoffs against updated_at. If these diverge, the
        dashboard's stall count and the campaign Stalled tab will disagree,
        which reads as a bug even though both would be internally consistent.
        """
        return or_(
            and_(
                CampaignCandidate.pipeline_stage == PipelineStage.SCREENING,
                CampaignCandidate.updated_at < now - timedelta(hours=screening_sla_hours),
            ),
            and_(
                CampaignCandidate.pipeline_stage == PipelineStage.HM_REVIEW,
                CampaignCandidate.updated_at < now - timedelta(days=hm_review_sla_days),
            ),
            and_(
                CampaignCandidate.pipeline_stage == PipelineStage.INTERVIEW,
                CampaignCandidate.updated_at < now - timedelta(days=interview_sla_days),
            ),
        )

    def _recruiter_campaign_ids(self, user_id: str):
        """
        "Campaigns I uploaded to" spans two independent upload paths -
        individual resumes and bulk ZIP jobs - plus campaigns the recruiter
        created. Unioned as a subquery so a recruiter who only ever
        bulk-uploaded is not reported as having zero campaigns.
        """
        from_resumes = (
            select(CampaignCandidate.campaign_id)
            .join(Resume, CampaignCandidate.resume_id == Resume.id)
            .where(Resume.uploaded_by == user_id)
        )
        from_bulk = select(BulkUploadJob.campaign_id).where(BulkUploadJob.uploaded_by == user_id)
        from_created = select(HiringCampaign.id).where(HiringCampaign.created_by == user_id)
        return from_resumes.union(from_bulk, from_created).subquery()

    def is_campaign_accessible_to_recruiter(self, user_id: str, campaign_id: UUID) -> bool:
        """
        Campaign-wide interview calendar follow-up - the first caller of
        _recruiter_campaign_ids outside this class. Reuses the exact same
        "campaigns I uploaded to or created" definition rather than the
        HiringCampaign.recruiter_id column, which exists on the model but
        is never actually checked against the acting user anywhere in this
        codebase - _recruiter_campaign_ids is the one real, established
        definition of "a recruiter's own campaigns."
        """
        stmt = select(HiringCampaign.id).where(
            HiringCampaign.id == campaign_id,
            HiringCampaign.id.in_(select(self._recruiter_campaign_ids(user_id))),
        ).limit(1)
        return self.db.execute(stmt).first() is not None

    # ── HR_ADMIN summary (S01-T02) ────────────────────────────────────

    def get_hr_admin_metrics(
        self,
        *,
        screening_sla_hours: float,
        hm_review_sla_days: int,
        interview_sla_days: int,
    ) -> dict:
        now = datetime.now(timezone.utc)
        seven_days_ago = now - timedelta(days=7)
        is_active = HiringCampaign.status == CampaignStatus.ACTIVE
        stalled = self._stall_predicate(
            now, screening_sla_hours, hm_review_sla_days, interview_sla_days
        )

        # single pass over campaign_candidates for all five candidate metrics
        row = (
            self.db.query(
                func.count(CampaignCandidate.id)
                .filter(CampaignCandidate.created_at >= seven_days_ago)
                .label("candidates_last_7_days"),
                func.count(CampaignCandidate.id)
                .filter(CampaignCandidate.pipeline_stage == PipelineStage.SHORTLISTED, is_active)
                .label("shortlisted_candidates"),
                func.count(CampaignCandidate.id)
                .filter(CampaignCandidate.pipeline_stage == PipelineStage.HM_REVIEW, is_active)
                .label("hm_review_pending"),
                # AI evaluation moved out to campaign_candidate_ai_evaluations,
                # so the failure count comes through an outer join rather than
                # a column on campaign_candidates.
                func.count(CampaignCandidateAIEvaluation.id)
                .filter(CampaignCandidateAIEvaluation.ai_evaluation_status == AIEvaluationStatus.FAILED)
                .label("ai_evaluation_failures"),
                func.count(distinct(CampaignCandidate.campaign_id))
                .filter(stalled, is_active)
                .label("campaigns_with_stall_warnings"),
            )
            .join(HiringCampaign, CampaignCandidate.campaign_id == HiringCampaign.id)
            .outerjoin(
                CampaignCandidateAIEvaluation,
                CampaignCandidateAIEvaluation.campaign_candidate_id == CampaignCandidate.id,
            )
            .one()
        )

        active_campaigns = (
            self.db.query(func.count(HiringCampaign.id))
            .filter(HiringCampaign.status == CampaignStatus.ACTIVE)
            .scalar()
        ) or 0

        pending_unknown_skills = (
            self.db.query(func.count(UnknownSkill.id))
            .filter(UnknownSkill.status == UnknownSkillStatus.PENDING)
            .scalar()
        ) or 0

        return {
            "active_campaigns": active_campaigns,
            "candidates_last_7_days": row.candidates_last_7_days or 0,
            "shortlisted_candidates": row.shortlisted_candidates or 0,
            "hm_review_pending": row.hm_review_pending or 0,
            "ai_evaluation_failures": row.ai_evaluation_failures or 0,
            "campaigns_with_stall_warnings": row.campaigns_with_stall_warnings or 0,
            "pending_unknown_skills": pending_unknown_skills,
        }

    def get_circuit_breaker_states(self) -> list[CircuitBreakerState]:
        return (
            self.db.query(CircuitBreakerState)
            .filter(CircuitBreakerState.service_name.in_(MONITORED_SERVICES))
            .order_by(CircuitBreakerState.service_name.asc())
            .all()
        )

    # ── RECRUITER summary (S01-T03) ───────────────────────────────────

    def get_recruiter_metrics(self, user_id: str) -> dict:
        now = datetime.now(timezone.utc)
        seven_days_ago = now - timedelta(days=7)

        campaigns_uploaded_to = (
            self.db.query(func.count()).select_from(self._recruiter_campaign_ids(user_id)).scalar()
        ) or 0

        campaigns_created = (
            self.db.query(func.count(HiringCampaign.id))
            .filter(HiringCampaign.created_by == user_id)
            .scalar()
        ) or 0

        resumes_last_7_days = (
            self.db.query(func.count(Resume.id))
            .filter(Resume.uploaded_by == user_id, Resume.created_at >= seven_days_ago)
            .scalar()
        ) or 0

        shortlisted_from_my_uploads = (
            self.db.query(func.count(CampaignCandidate.id))
            .join(Resume, CampaignCandidate.resume_id == Resume.id)
            .filter(
                Resume.uploaded_by == user_id,
                CampaignCandidate.pipeline_stage == PipelineStage.SHORTLISTED,
            )
            .scalar()
        ) or 0

        failed_bulk_jobs = (
            self.db.query(func.count(BulkUploadJob.id))
            .filter(
                BulkUploadJob.uploaded_by == user_id,
                BulkUploadJob.status.in_(
                    [BulkUploadStatus.FAILED, BulkUploadStatus.PARTIAL_FAILURE]
                ),
            )
            .scalar()
        ) or 0

        return {
            "campaigns_uploaded_to": campaigns_uploaded_to,
            "campaigns_created": campaigns_created,
            "resumes_last_7_days": resumes_last_7_days,
            "shortlisted_from_my_uploads": shortlisted_from_my_uploads,
            "failed_bulk_jobs": failed_bulk_jobs,
        }

    # ── campaign cards (S02-T01 + S03-T01) ────────────────────────────

    def get_campaign_cards(
        self,
        *,
        recruiter_id: str | None,
        show_closed: bool,
        limit: int,
        screening_sla_hours: float,
        hm_review_sla_days: int,
        interview_sla_days: int,
        search: str | None = None,
        status: CampaignStatus | None = None,
        hiring_manager_id: str | None = None,
    ):
        """
        Every card and all six of its counts in ONE grouped query.

        The existing campaign list endpoints issue a handful of follow-up
        counts per campaign row; on a dashboard showing many campaigns that
        pattern is what makes the page slow, so it is not reused here.
        recruiter_id=None means "all campaigns" (HR_ADMIN).
        """
        now = datetime.now(timezone.utc)
        stalled = self._stall_predicate(
            now, screening_sla_hours, hm_review_sla_days, interview_sla_days
        )

        stmt = (
            select(
                HiringCampaign.id,
                HiringCampaign.name,
                HiringCampaign.status,
                HiringCampaign.max_candidates,
                HiringCampaign.deadline,
                HiringCampaign.created_at,
                HiringCampaign.hiring_manager_id,
                HiringCampaign.jd_id,
                JobDescription.title.label("jd_title"),
                JobDescription.version_number.label("jd_version"),
                func.count(CampaignCandidate.id).label("candidate_count"),
                func.count(CampaignCandidate.id)
                .filter(CampaignCandidate.pipeline_stage == PipelineStage.SHORTLISTED)
                .label("shortlisted_count"),
                func.count(CampaignCandidate.id)
                .filter(CampaignCandidate.pipeline_stage == PipelineStage.SELECTED)
                .label("selected_count"),
                func.count(CampaignCandidate.id)
                .filter(CampaignCandidate.pipeline_stage == PipelineStage.HM_REVIEW)
                .label("hm_review_count"),
                func.count(CampaignCandidateAIEvaluation.id)
                .filter(CampaignCandidateAIEvaluation.ai_evaluation_status == AIEvaluationStatus.FAILED)
                .label("ai_failure_count"),
                func.count(CampaignCandidate.id).filter(stalled).label("stalled_count"),
            )
            .select_from(HiringCampaign)
            .outerjoin(JobDescription, JobDescription.id == HiringCampaign.jd_id)
            .outerjoin(CampaignCandidate, CampaignCandidate.campaign_id == HiringCampaign.id)
            .outerjoin(
                CampaignCandidateAIEvaluation,
                CampaignCandidateAIEvaluation.campaign_candidate_id == CampaignCandidate.id,
            )
            .group_by(
                HiringCampaign.id,
                JobDescription.title,
                JobDescription.version_number,
            )
            .order_by(HiringCampaign.created_at.desc())
            .limit(limit)
        )

        if not show_closed:
            stmt = stmt.where(HiringCampaign.status != CampaignStatus.CLOSED)
        if recruiter_id is not None:
            stmt = stmt.where(
                HiringCampaign.id.in_(select(self._recruiter_campaign_ids(recruiter_id)))
            )
        # S02-T03: search spans campaign name AND the linked JD title, so
        # "python" finds campaigns named for the role as well as those merely
        # linked to a Python JD.
        if search:
            pattern = f"%{search.strip()}%"
            stmt = stmt.where(
                or_(HiringCampaign.name.ilike(pattern), JobDescription.title.ilike(pattern))
            )
        if status is not None:
            stmt = stmt.where(HiringCampaign.status == status)
        if hiring_manager_id:
            stmt = stmt.where(HiringCampaign.hiring_manager_id == hiring_manager_id)

        return self.db.execute(stmt).all()

    # ── nav badges (S03-T03) ──────────────────────────────────────────

    def get_nav_badge_counts(self, recruiter_id: str | None) -> dict:
        """
        The three cross-campaign counts the nav polls. One query, three
        FILTERed counts - a separate query per badge would triple the load of
        something that refreshes every 60 seconds.
        """
        stmt = (
            select(
                func.count(CampaignCandidate.id)
                .filter(CampaignCandidate.pipeline_stage == PipelineStage.SHORTLISTED)
                .label("pending_reviews"),
                func.count(CampaignCandidate.id)
                .filter(
                    CampaignCandidate.is_fraud_flagged.is_(True),
                    CampaignCandidate.pipeline_stage == PipelineStage.FRAUD_REVIEW,
                )
                .label("fraud_review"),
                func.count(CampaignCandidateAIEvaluation.id)
                .filter(CampaignCandidateAIEvaluation.ai_evaluation_status == AIEvaluationStatus.FAILED)
                .label("ai_failures"),
            )
            .select_from(CampaignCandidate)
            .outerjoin(
                CampaignCandidateAIEvaluation,
                CampaignCandidateAIEvaluation.campaign_candidate_id == CampaignCandidate.id,
            )
        )
        if recruiter_id is not None:
            stmt = stmt.where(
                CampaignCandidate.campaign_id.in_(
                    select(self._recruiter_campaign_ids(recruiter_id))
                )
            )
        row = self.db.execute(stmt).one()
        return {
            "pending_reviews": row.pending_reviews or 0,
            "fraud_review": row.fraud_review or 0,
            "ai_failures": row.ai_failures or 0,
        }

    # ── stage timing metrics (S04-T02) ────────────────────────────────

    def get_stage_timing(self, campaign_id: UUID):
        """
        Average and maximum days candidates have been sitting in their CURRENT
        stage, derived from the most recent stage-history row per candidate.

        DISTINCT ON gives the latest transition per candidate in one pass; the
        alternative (a correlated MAX(changed_at) subquery per row) is markedly
        slower on campaigns with long histories.
        """
        latest = (
            select(
                CampaignCandidateStageHistory.campaign_candidate_id.label("cc_id"),
                CampaignCandidateStageHistory.to_stage.label("stage"),
                CampaignCandidateStageHistory.changed_at.label("entered_at"),
            )
            .join(
                CampaignCandidate,
                CampaignCandidate.id == CampaignCandidateStageHistory.campaign_candidate_id,
            )
            .where(CampaignCandidate.campaign_id == campaign_id)
            .distinct(CampaignCandidateStageHistory.campaign_candidate_id)
            .order_by(
                CampaignCandidateStageHistory.campaign_candidate_id,
                CampaignCandidateStageHistory.changed_at.desc(),
            )
            .subquery()
        )

        now = func.now()
        seconds_in_stage = func.extract("epoch", now - latest.c.entered_at) / 86400.0

        return self.db.execute(
            select(
                latest.c.stage,
                func.count().label("candidate_count"),
                func.avg(seconds_in_stage).label("avg_days"),
                func.max(seconds_in_stage).label("max_days"),
            )
            .group_by(latest.c.stage)
        ).all()

    def get_user_names(self, user_ids: list[str]) -> dict[str, str]:
        ids = [uid for uid in set(user_ids) if uid]
        if not ids:
            return {}
        rows = self.db.query(User.id, User.full_name).filter(User.id.in_(ids)).all()
        return {str(uid): name for uid, name in rows}

    def get_last_login_at(self, user_id: str):
        return self.db.query(User.last_login_at).filter(User.id == user_id).scalar()
