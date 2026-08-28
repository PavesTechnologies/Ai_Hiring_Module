from datetime import datetime

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.campaigns import CampaignStatus, HiringCampaign
from app.models.candidates import Candidate, ParseStatus, Resume
from app.models.compliance import AuditLog
from app.models.identity import User
from app.models.pipeline import CampaignCandidate, DecisionType, PipelineStage


class DashboardRepository:
    """
    Platform-wide aggregate queries backing the dashboard overview screen —
    distinct from CampaignRepository's queries, which are always scoped to
    one campaign_id.
    """

    def __init__(self, db: Session):
        self.db = db

    def count_campaigns(self,
        status: CampaignStatus | None = None,
        created_after: datetime | None = None,
    ) -> int:
        query = self.db.query(func.count(HiringCampaign.id))
        if status is not None:
            query = query.filter(HiringCampaign.status == status)
        if created_after is not None:
            query = query.filter(HiringCampaign.created_at >= created_after)
        return query.scalar() or 0

    def count_candidates_in_pipeline(self,
        created_after: datetime | None = None,
    ) -> int:
        """
        Candidates still actively moving through an ACTIVE campaign — excludes
        the two terminal stages (SELECTED/REJECTED), which are outcomes, not
        work-in-progress.
        """
        query = (self.db.query(func.count(CampaignCandidate.id))
            .join(HiringCampaign, CampaignCandidate.campaign_id == HiringCampaign.id)
            .filter(HiringCampaign.status == CampaignStatus.ACTIVE)
            .filter(CampaignCandidate.pipeline_stage.notin_(
                [PipelineStage.SELECTED, PipelineStage.REJECTED],
            ))
        )
        if created_after is not None:
            query = query.filter(CampaignCandidate.created_at >= created_after)
        return query.scalar() or 0

    def count_selected(self,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> int:
        query = self.db.query(func.count(CampaignCandidate.id)).filter(
            CampaignCandidate.decision_type == DecisionType.SELECTED,
        )
        if since is not None:
            query = query.filter(CampaignCandidate.decision_at >= since)
        if until is not None:
            query = query.filter(CampaignCandidate.decision_at < until)
        return query.scalar() or 0

    def avg_time_to_hire_days(self,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> float | None:
        """
        Approximation: candidate_candidates.created_at (entry into the
        campaign) -> decision_at for a SELECTED decision. Not a true
        "requisition opened -> offer accepted" measure — there is no offer/
        acceptance concept in the schema yet.
        """
        query = self.db.query(
            func.avg(
                func.extract("epoch", CampaignCandidate.decision_at - CampaignCandidate.created_at) / 86400.0,
            ),
        ).filter(
            CampaignCandidate.decision_type == DecisionType.SELECTED,
            CampaignCandidate.decision_at.isnot(None),
        )
        if since is not None:
            query = query.filter(CampaignCandidate.decision_at >= since)
        if until is not None:
            query = query.filter(CampaignCandidate.decision_at < until)
        result = query.scalar()
        return float(result) if result is not None else None

    def count_uploaded_resumes(self, created_after: datetime | None = None) -> int:
        query = self.db.query(func.count(Resume.id))
        if created_after is not None:
            query = query.filter(Resume.created_at >= created_after)
        return query.scalar() or 0

    def count_parsing_resumes(self, created_after: datetime | None = None) -> int:
        query = self.db.query(func.count(Resume.id)).filter(
            Resume.parse_status.in_([ParseStatus.PENDING, ParseStatus.PARSING]),
        )
        if created_after is not None:
            query = query.filter(Resume.created_at >= created_after)
        return query.scalar() or 0

    def get_pipeline_stage_counts(self,
        created_after: datetime | None = None,
        campaign_status: CampaignStatus = CampaignStatus.ACTIVE,
    ) -> dict[str, int]:
        query = (self.db.query(CampaignCandidate.pipeline_stage, func.count())
            .join(HiringCampaign, CampaignCandidate.campaign_id == HiringCampaign.id)
            .filter(HiringCampaign.status == campaign_status)
        )
        if created_after is not None:
            query = query.filter(CampaignCandidate.created_at >= created_after)
        rows = query.group_by(CampaignCandidate.pipeline_stage).all()
        return {stage.value: count for stage, count in rows}

    def get_top_candidates(self,
        limit: int,
        campaign_status: CampaignStatus = CampaignStatus.ACTIVE,
    ) -> list[tuple[CampaignCandidate, Candidate, Resume | None, HiringCampaign]]:
        return (self.db.query(CampaignCandidate, Candidate, Resume, HiringCampaign)
            .join(Candidate, CampaignCandidate.candidate_id == Candidate.id)
            .outerjoin(Resume, CampaignCandidate.resume_id == Resume.id)
            .join(HiringCampaign, CampaignCandidate.campaign_id == HiringCampaign.id)
            .filter(HiringCampaign.status == campaign_status)
            .filter(CampaignCandidate.composite_score.isnot(None))
            .order_by(CampaignCandidate.composite_score.desc())
            .limit(limit)
            .all()
        )

    def get_recent_audit_entries(self,
        limit: int,
        action_types: list,
    ) -> list[AuditLog]:
        return (self.db.query(AuditLog)
            .filter(AuditLog.action_type.in_(action_types))
            .order_by(AuditLog.created_at.desc())
            .limit(limit)
            .all()
        )

    def get_user_names(self, user_ids: list[str]) -> dict[str, str]:
        if not user_ids:
            return {}
        rows = (self.db.query(User.id, User.full_name)
            .filter(User.id.in_(user_ids))
            .all()
        )
        return {str(user_id): full_name for user_id, full_name in rows}
