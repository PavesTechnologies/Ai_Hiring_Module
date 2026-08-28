import uuid
from datetime import datetime

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.campaigns import CampaignStatus, HiringCampaign
from app.models.candidates import Candidate, ParseStatus, Resume
from app.models.compliance import AuditLog
from app.models.identity import User
from app.models.pipeline import (
    CampaignCandidate, CampaignCandidateStageHistory, DecisionType, PipelineStage,
)
from app.models.skills import CandidateSkill, SkillOntology


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

    # ── Candidate filter bar (CampaignDetails "More filters") ──────────

    def search_campaign_skills(self,
        campaign_id: uuid.UUID,
        search: str | None,
        limit: int,
    ) -> list[tuple[uuid.UUID, str, str | None, int]]:
        """
        Skill-name autocomplete scoped to this campaign — only skills that
        appear on the specific resume version each candidate submitted here
        (CandidateSkill.resume_id == CampaignCandidate.resume_id), not every
        historical resume the person has ever uploaded to any campaign.
        """
        query = (self.db.query(
                SkillOntology.id,
                SkillOntology.canonical_name,
                SkillOntology.category,
                func.count(func.distinct(CandidateSkill.candidate_id)),
            )
            .join(CandidateSkill, CandidateSkill.canonical_skill_id == SkillOntology.id)
            .join(CampaignCandidate, CampaignCandidate.resume_id == CandidateSkill.resume_id)
            .filter(CampaignCandidate.campaign_id == campaign_id)
        )
        if search:
            query = query.filter(SkillOntology.canonical_name.ilike(f"%{search.strip()}%"))
        return (query
            .group_by(SkillOntology.id, SkillOntology.canonical_name, SkillOntology.category)
            .order_by(func.count(func.distinct(CandidateSkill.candidate_id)).desc(), SkillOntology.canonical_name.asc())
            .limit(limit)
            .all()
        )

    def filter_campaign_candidates_by_skills(self,
        campaign_id: uuid.UUID,
        skill_ids: list[uuid.UUID],
    ) -> list[uuid.UUID]:
        """
        AND match: campaign candidates whose submitted resume holds every one
        of the given canonical skill ids (GROUP BY ... HAVING COUNT DISTINCT
        == len(skill_ids), same convention CampaignDetails' filter docs
        already describe).
        """
        if not skill_ids:
            return []
        rows = (self.db.query(CampaignCandidate.id)
            .join(CandidateSkill, CandidateSkill.resume_id == CampaignCandidate.resume_id)
            .filter(CampaignCandidate.campaign_id == campaign_id)
            .filter(CandidateSkill.canonical_skill_id.in_(skill_ids))
            .group_by(CampaignCandidate.id)
            .having(func.count(func.distinct(CandidateSkill.canonical_skill_id)) == len(skill_ids))
            .all()
        )
        return [row[0] for row in rows]

    def get_campaign_candidates_for_resume_filter(self,
        campaign_id: uuid.UUID,
        *,
        uploaded_by: str | None = None,
        upload_type: str | None = None,
        uploaded_from: datetime | None = None,
        uploaded_to: datetime | None = None,
    ) -> list[tuple[uuid.UUID, dict | None]]:
        """
        Every campaign candidate's (id, resume.parsed_json), pre-filtered by
        whatever SQL-pushable resume columns were given (uploader, upload
        source, upload date). Experience/education live in parsed_json as
        free-text JSONB with no normalized column, so those two filters are
        applied in Python by the caller — mirrors DashboardService's own
        parsed_json reads elsewhere (e.g. _extract_designation_and_experience)
        rather than introducing a first JSONB-path SQL filter with no
        precedent in this codebase.
        """
        query = (self.db.query(CampaignCandidate.id, Resume.parsed_json)
            .join(Resume, CampaignCandidate.resume_id == Resume.id)
            .filter(CampaignCandidate.campaign_id == campaign_id)
        )
        if uploaded_by:
            query = query.filter(Resume.uploaded_by == uploaded_by)
        if upload_type == "individual":
            query = query.filter(Resume.bulk_upload_job_id.is_(None))
        elif upload_type == "bulk":
            query = query.filter(Resume.bulk_upload_job_id.isnot(None))
        if uploaded_from is not None:
            query = query.filter(Resume.created_at >= uploaded_from)
        if uploaded_to is not None:
            query = query.filter(Resume.created_at < uploaded_to)
        return query.all()

    def get_campaign_uploaders(self, campaign_id: uuid.UUID) -> list[tuple[str, int]]:
        return (self.db.query(Resume.uploaded_by, func.count(CampaignCandidate.id))
            .join(CampaignCandidate, CampaignCandidate.resume_id == Resume.id)
            .filter(CampaignCandidate.campaign_id == campaign_id)
            .group_by(Resume.uploaded_by)
            .order_by(func.count(CampaignCandidate.id).desc())
            .all()
        )

    def get_campaign_stage_timing(self, campaign_id: uuid.UUID) -> list[tuple[str, float, float]]:
        """
        Average/max days spent in each stage, derived from
        campaign_candidate_stage_history: for every transition INTO a stage,
        LEAD() finds that same candidate's NEXT transition, and the gap
        between the two is the time spent in that stage. A candidate still
        currently sitting in a stage (no next transition yet) has no gap and
        is excluded — their time-in-stage isn't over yet, so it would
        understate the real duration if counted now.
        """
        duration_days = (
            func.extract(
                "epoch",
                func.lead(CampaignCandidateStageHistory.changed_at).over(
                    partition_by=CampaignCandidateStageHistory.campaign_candidate_id,
                    order_by=CampaignCandidateStageHistory.changed_at,
                ) - CampaignCandidateStageHistory.changed_at,
            ) / 86400.0
        ).label("duration_days")

        subquery = (self.db.query(
                CampaignCandidateStageHistory.to_stage.label("stage"),
                duration_days,
            )
            .join(CampaignCandidate, CampaignCandidate.id == CampaignCandidateStageHistory.campaign_candidate_id)
            .filter(CampaignCandidate.campaign_id == campaign_id)
            .subquery()
        )

        return (self.db.query(
                subquery.c.stage,
                func.avg(subquery.c.duration_days),
                func.max(subquery.c.duration_days),
            )
            .filter(subquery.c.duration_days.isnot(None))
            .group_by(subquery.c.stage)
            .all()
        )
