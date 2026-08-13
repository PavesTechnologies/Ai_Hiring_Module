from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.campaigns import HiringCampaign
from app.models.candidates import Candidate, Resume
from app.models.compliance import AuditLog, CandidateConsent
from app.models.jd.job_descriptions import JobDescription
from app.models.pipeline import (
    CampaignCandidate,
    CampaignCandidateAIEvaluation,
    CampaignCandidateStageHistory,
    DecisionType,
    PipelineStage,
)
from app.models.skills import CandidateSkill


class ExportRepository:
    """
    Export read model.

    Note on the spec's DB references: it names `candidate_rejections` and
    `score_breakdown`, neither of which exists in this schema — the unified
    decision model replaced them, so rejection facts come from
    campaign_candidates.decision_* and the score layers live on the
    campaign_candidate row plus campaign_candidate_ai_evaluations.
    """

    def __init__(self, db: Session):
        self.db = db

    # ── candidate list ────────────────────────────────────────────────

    def candidates_for_export(
        self,
        campaign_id: UUID,
        *,
        campaign_candidate_ids: list[UUID] | None = None,
        pipeline_stage: PipelineStage | None = None,
    ):
        """
        One row per candidate with the AI evaluation joined. LEFT JOIN, not
        INNER: a candidate rejected before the AI layer ran has no evaluation
        row, and dropping them would silently shrink the export.
        """
        stmt = (
            select(
                CampaignCandidate.id.label("campaign_candidate_id"),
                CampaignCandidate.candidate_id,
                CampaignCandidate.resume_id,
                CampaignCandidate.pipeline_stage,
                CampaignCandidate.composite_score,
                CampaignCandidate.deterministic_score,
                CampaignCandidate.semantic_score,
                CampaignCandidate.deterministic_passed,
                CampaignCandidate.semantic_passed,
                CampaignCandidate.is_fraud_flagged,
                CampaignCandidate.decision_type,
                CampaignCandidate.decision_source,
                CampaignCandidate.decision_reason,
                CampaignCandidate.decision_details,
                CampaignCandidate.decision_at,
                CampaignCandidate.created_at,
                CampaignCandidateAIEvaluation.ai_ats_score,
                CampaignCandidateAIEvaluation.effective_ai_score,
                CampaignCandidateAIEvaluation.ai_confidence,
                CampaignCandidateAIEvaluation.ai_recommendation,
                CampaignCandidateAIEvaluation.ai_strengths,
                CampaignCandidateAIEvaluation.ai_weaknesses,
                CampaignCandidateAIEvaluation.ai_evaluation_status,
                CampaignCandidateAIEvaluation.prompt_version_id,
                Resume.uploaded_by,
            )
            .outerjoin(
                CampaignCandidateAIEvaluation,
                CampaignCandidateAIEvaluation.campaign_candidate_id == CampaignCandidate.id,
            )
            .outerjoin(Resume, Resume.id == CampaignCandidate.resume_id)
            .where(CampaignCandidate.campaign_id == campaign_id)
            # Same ranking order the list screen uses, so the export matches
            # what the user was looking at when they pressed Export.
            .order_by(
                CampaignCandidate.composite_score.desc().nullslast(),
                CampaignCandidate.deterministic_score.desc().nullslast(),
                CampaignCandidate.created_at.asc(),
                CampaignCandidate.id.asc(),
            )
        )
        if campaign_candidate_ids is not None:
            stmt = stmt.where(CampaignCandidate.id.in_(campaign_candidate_ids))
        if pipeline_stage is not None:
            stmt = stmt.where(CampaignCandidate.pipeline_stage == pipeline_stage)
        return self.db.execute(stmt).all()

    def days_in_current_stage(self, campaign_id: UUID) -> dict[str, int]:
        """
        {campaign_candidate_id: days since entering its current stage}, computed
        from the newest stage-history row per candidate in one pass rather than
        a query per candidate.
        """
        newest = (
            select(
                CampaignCandidateStageHistory.campaign_candidate_id.label("cc_id"),
                func.max(CampaignCandidateStageHistory.changed_at).label("entered_at"),
            )
            .join(
                CampaignCandidate,
                CampaignCandidate.id == CampaignCandidateStageHistory.campaign_candidate_id,
            )
            .where(CampaignCandidate.campaign_id == campaign_id)
            .group_by(CampaignCandidateStageHistory.campaign_candidate_id)
            .subquery()
        )
        now = datetime.now(timezone.utc)
        out: dict[str, int] = {}
        for row in self.db.execute(select(newest.c.cc_id, newest.c.entered_at)).all():
            if row.entered_at is None:
                continue
            entered = row.entered_at
            if entered.tzinfo is None:
                entered = entered.replace(tzinfo=timezone.utc)
            out[str(row.cc_id)] = (now - entered).days
        return out

    def rejection_rows(self, campaign_id: UUID):
        """
        Every rejection event for the campaign, read from stage history
        rather than the current decision alone: a candidate re-evaluated and
        rejected twice must appear twice, which a single decision_* snapshot
        cannot express.
        """
        stmt = (
            select(
                CampaignCandidate.id.label("campaign_candidate_id"),
                CampaignCandidate.candidate_id,
                CampaignCandidateStageHistory.changed_at.label("rejected_at"),
                CampaignCandidateStageHistory.change_reason,
                CampaignCandidateStageHistory.transition_source,
                CampaignCandidate.decision_type,
                CampaignCandidate.decision_source,
                CampaignCandidate.decision_reason,
                CampaignCandidate.decision_details,
                CampaignCandidate.pipeline_stage,
            )
            .join(
                CampaignCandidateStageHistory,
                CampaignCandidateStageHistory.campaign_candidate_id == CampaignCandidate.id,
            )
            .where(
                CampaignCandidate.campaign_id == campaign_id,
                CampaignCandidateStageHistory.to_stage == PipelineStage.REJECTED,
            )
            .order_by(
                CampaignCandidate.id.asc(),
                CampaignCandidateStageHistory.changed_at.asc(),
            )
        )
        return self.db.execute(stmt).all()

    # ── scorecard ─────────────────────────────────────────────────────

    def scorecard_skills(self, resume_id: UUID):
        if resume_id is None:
            return []
        return (
            self.db.query(CandidateSkill)
            .filter(CandidateSkill.resume_id == resume_id)
            .all()
        )

    def resume_for(self, resume_id: UUID):
        if resume_id is None:
            return None
        return self.db.query(Resume).filter(Resume.id == resume_id).first()

    def stage_history_for(self, campaign_candidate_id: UUID):
        return (
            self.db.query(CampaignCandidateStageHistory)
            .filter(CampaignCandidateStageHistory.campaign_candidate_id == campaign_candidate_id)
            .order_by(CampaignCandidateStageHistory.changed_at.asc())
            .all()
        )

    # ── audit & compliance ────────────────────────────────────────────

    def audit_events(self, campaign_id: UUID):
        return (
            self.db.query(AuditLog)
            .filter(AuditLog.campaign_id == campaign_id)
            .order_by(AuditLog.created_at.asc())
            .all()
        )

    def stage_transitions(self, campaign_id: UUID):
        stmt = (
            select(
                CampaignCandidate.candidate_id,
                CampaignCandidateStageHistory.from_stage,
                CampaignCandidateStageHistory.to_stage,
                CampaignCandidateStageHistory.changed_by,
                CampaignCandidateStageHistory.change_reason,
                CampaignCandidateStageHistory.transition_source,
                CampaignCandidateStageHistory.changed_at,
                CampaignCandidateStageHistory.scores_snapshot,
            )
            .join(
                CampaignCandidate,
                CampaignCandidate.id == CampaignCandidateStageHistory.campaign_candidate_id,
            )
            .where(CampaignCandidate.campaign_id == campaign_id)
            .order_by(CampaignCandidateStageHistory.changed_at.asc())
        )
        return self.db.execute(stmt).all()

    def layer_counts(self, campaign_id: UUID) -> dict:
        """
        Screening funnel: how many candidates passed/failed each layer.
        FILTER beats several COUNT round trips and keeps the percentages
        internally consistent by construction.
        """
        row = self.db.execute(
            select(
                func.count(CampaignCandidate.id).label("total"),
                func.count(CampaignCandidate.id).filter(
                    CampaignCandidate.deterministic_passed.is_(True)).label("det_pass"),
                func.count(CampaignCandidate.id).filter(
                    CampaignCandidate.deterministic_passed.is_(False)).label("det_fail"),
                func.count(CampaignCandidate.id).filter(
                    CampaignCandidate.semantic_passed.is_(True)).label("sem_pass"),
                func.count(CampaignCandidate.id).filter(
                    CampaignCandidate.semantic_passed.is_(False)).label("sem_fail"),
                func.count(CampaignCandidate.id).filter(
                    CampaignCandidate.pipeline_stage == PipelineStage.REJECTED).label("rejected"),
                func.count(CampaignCandidate.id).filter(
                    CampaignCandidate.decision_type == DecisionType.RESET).label("overridden"),
                func.count(CampaignCandidate.id).filter(
                    CampaignCandidate.is_fraud_flagged.is_(True)).label("fraud"),
            ).where(CampaignCandidate.campaign_id == campaign_id)
        ).first()
        return dict(row._mapping) if row else {}

    def ai_recommendation_counts(self, campaign_id: UUID) -> dict[str, int]:
        rows = self.db.execute(
            select(
                CampaignCandidateAIEvaluation.ai_recommendation,
                func.count(CampaignCandidateAIEvaluation.id),
            )
            .join(
                CampaignCandidate,
                CampaignCandidate.id == CampaignCandidateAIEvaluation.campaign_candidate_id,
            )
            .where(CampaignCandidate.campaign_id == campaign_id)
            .group_by(CampaignCandidateAIEvaluation.ai_recommendation)
        ).all()
        return {
            (r[0].value if hasattr(r[0], "value") else str(r[0])): r[1]
            for r in rows if r[0] is not None
        }

    def rejection_reason_distribution(self, campaign_id: UUID, limit: int = 15):
        """Aggregate only — deliberately carries no candidate identifiers."""
        rows = self.db.execute(
            select(
                CampaignCandidate.decision_source,
                CampaignCandidate.decision_reason,
                func.count(CampaignCandidate.id).label("cnt"),
            )
            .where(
                CampaignCandidate.campaign_id == campaign_id,
                CampaignCandidate.pipeline_stage == PipelineStage.REJECTED,
            )
            .group_by(CampaignCandidate.decision_source, CampaignCandidate.decision_reason)
            .order_by(func.count(CampaignCandidate.id).desc())
            .limit(limit)
        ).all()
        return rows

    def manual_interventions(self, campaign_id: UUID):
        """
        Actor ROLE only, never individual names — reviewer
        privacy at aggregate level. Grouped so no name can leak.
        """
        rows = self.db.execute(
            select(
                AuditLog.action_type,
                AuditLog.actor_role,
                func.count(AuditLog.id).label("cnt"),
            )
            .where(AuditLog.campaign_id == campaign_id)
            .group_by(AuditLog.action_type, AuditLog.actor_role)
            .order_by(func.count(AuditLog.id).desc())
        ).all()
        return rows

    # ── export history ────────────────────────────────────────

    def export_history(self, campaign_id: UUID, limit: int = 20):
        """
        Past EXPORT_GENERATE runs for one campaign, newest first.

        Matched on the idempotency_key prefix because celery_task_log has no
        campaign_id column; export_tasks.export_idempotency_key writes the
        campaign into that key precisely so this lookup is possible without a
        schema change.
        """
        from app.models.async_tasks import CeleryTaskLog

        return (
            self.db.query(CeleryTaskLog)
            .filter(
                CeleryTaskLog.task_type == "EXPORT_GENERATE",
                CeleryTaskLog.idempotency_key.like(f"EXPORT_GENERATE:{campaign_id}:%"),
            )
            .order_by(CeleryTaskLog.queued_at.desc())
            .limit(limit)
            .all()
        )

    # ── DSAR ──────────────────────────────────────────────────

    def candidate_by_email_hash(self, email_hash: str):
        return (
            self.db.query(Candidate)
            .filter(Candidate.email_hash == email_hash)
            .first()
        )

    def dsar_bundle(self, candidate_id: UUID) -> dict:
        """Everything held about one candidate, across every campaign."""
        resumes = self.db.query(Resume).filter(Resume.candidate_id == candidate_id).all()
        appearances = self.db.execute(
            select(
                CampaignCandidate.id,
                CampaignCandidate.campaign_id,
                HiringCampaign.name.label("campaign_name"),
                JobDescription.title.label("jd_title"),
                CampaignCandidate.pipeline_stage,
                CampaignCandidate.composite_score,
                CampaignCandidate.deterministic_score,
                CampaignCandidate.semantic_score,
                CampaignCandidate.decision_type,
                CampaignCandidate.decision_source,
                CampaignCandidate.decision_reason,
                CampaignCandidate.created_at,
            )
            .join(HiringCampaign, HiringCampaign.id == CampaignCandidate.campaign_id)
            .outerjoin(JobDescription, JobDescription.id == HiringCampaign.jd_id)
            .where(CampaignCandidate.candidate_id == candidate_id)
            .order_by(CampaignCandidate.created_at.asc())
        ).all()
        resume_ids = [r.id for r in resumes]
        skills = (
            self.db.query(CandidateSkill)
            .filter(CandidateSkill.resume_id.in_(resume_ids))
            .all()
            if resume_ids else []
        )
        consents = (
            self.db.query(CandidateConsent)
            .filter(CandidateConsent.candidate_id == candidate_id)
            .order_by(CandidateConsent.consent_timestamp.asc())
            .all()
        )
        return {
            "resumes": resumes,
            "appearances": appearances,
            "skills": skills,
            "consents": consents,
        }
