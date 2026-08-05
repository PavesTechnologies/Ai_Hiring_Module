from datetime import datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.campaigns import HiringCampaign
from app.models.candidates import Candidate, Resume
from app.models.jd.job_descriptions import JobDescription
from app.models.pipeline import (
    AIEvaluationStatus,
    CampaignCandidate,
    CampaignCandidateStageHistory,
    PipelineStage,
    TransitionSource,
)


class CampaignCandidateRepository:

    def __init__(self, db: Session):
        self.db = db

    def create(
        self,
        campaign_candidate: CampaignCandidate,
    ) -> CampaignCandidate:
        """
        Create a new campaign candidate.
        """
        self.db.add(campaign_candidate)
        self.db.flush()
        self.db.refresh(campaign_candidate)
        return campaign_candidate

    def get_by_idempotency_key(
        self,
        idempotency_key: str,
    ) -> CampaignCandidate | None:
        return (
            self.db.query(CampaignCandidate)
            .filter(CampaignCandidate.idempotency_key == idempotency_key)
            .first()
        )

    def create_idempotent(
        self,
        campaign_candidate: CampaignCandidate,
    ) -> tuple[CampaignCandidate, bool]:
        """
        Attempts to insert `campaign_candidate`. A retried request (same
        idempotency_key) or two concurrent requests computing the same key
        can both attempt an insert — idempotency_key is unique, so the
        loser's flush raises IntegrityError. A SAVEPOINT scopes that to just
        this insert attempt (same pattern as
        SkillRepository.upsert_unknown_skill and
        CandidateRepository.create), then falls back to the existing row
        instead of raising. Returns (campaign_candidate, was_created).
        """
        try:
            with self.db.begin_nested():
                self.db.add(campaign_candidate)
                self.db.flush()
            self.db.refresh(campaign_candidate)
            return campaign_candidate, True
        except IntegrityError:
            existing = self.get_by_idempotency_key(campaign_candidate.idempotency_key)
            return existing, False

    def create_stage_history(
        self,
        campaign_candidate_id: UUID,
        to_stage: PipelineStage,
        from_stage: PipelineStage | None = None,
        changed_by: str | None = None,
        change_reason: str | None = None,
        transition_source: TransitionSource = TransitionSource.SYSTEM,
        scores_snapshot: dict | None = None,
    ) -> CampaignCandidateStageHistory:
        history = CampaignCandidateStageHistory(
            campaign_candidate_id=campaign_candidate_id,
            from_stage=from_stage,
            to_stage=to_stage,
            changed_by=changed_by,
            change_reason=change_reason,
            transition_source=transition_source,
            scores_snapshot=scores_snapshot,
        )
        self.db.add(history)
        self.db.flush()
        self.db.refresh(history)
        return history

    def update_pipeline_stage(
        self,
        campaign_candidate: CampaignCandidate,
        to_stage: PipelineStage,
    ) -> CampaignCandidate:
        """
        Epic 3 (M05-E03) Phase C0 — mutates the already-loaded row directly
        and flushes, matching this repo's existing single-field-update style
        (create_stage_history, etc.) rather than a raw atomic UPDATE: unlike
        the bulk job counters, a stage transition isn't a concurrent-increment
        scenario, so there's no lost-update race to guard against here.
        """
        campaign_candidate.pipeline_stage = to_stage
        self.db.flush()
        self.db.refresh(campaign_candidate)
        return campaign_candidate

    def get_by_id(
        self,
        campaign_candidate_id: UUID,
    ) -> CampaignCandidate | None:
        """
        Get campaign candidate by ID.
        """
        return (
            self.db.query(CampaignCandidate)
            .filter(CampaignCandidate.id == campaign_candidate_id)
            .first()
        )
    
    def get_by_candidate_id(
        self,
        candidate_id: UUID,
    ) -> list[CampaignCandidate]:
        """
        Candidate erasure — every campaign_candidates row for this candidate
        across every campaign they were ever submitted to (a candidate can
        appear in more than one campaign).
        """
        return (
            self.db.query(CampaignCandidate)
            .filter(CampaignCandidate.candidate_id == candidate_id)
            .all()
        )

    def delete_stage_history(
        self,
        campaign_candidate_id: UUID,
    ) -> None:
        """Candidate erasure — removes campaign_candidate_stage_history rows for one campaign_candidate."""
        self.db.execute(
            delete(CampaignCandidateStageHistory)
            .where(CampaignCandidateStageHistory.campaign_candidate_id == campaign_candidate_id)
        )
        self.db.flush()

    def get_by_resume_id(
        self,
        resume_id: UUID,
    ) -> list[CampaignCandidate]:
        """
        Every campaign_candidates row pointing at this resume - normally
        exactly one (a resume upload is always for one specific campaign),
        but a resume can in principle be reused across campaigns.
        """
        return (
            self.db.query(CampaignCandidate)
            .filter(CampaignCandidate.resume_id == resume_id)
            .all()
        )

    def get_by_resume_ids(
        self,
        resume_ids: list[UUID],
        campaign_id: UUID | None = None,
    ) -> list[CampaignCandidate]:
        """
        Batched counterpart to get_by_resume_id - one query for a whole
        list page's worth of resumes instead of one query per row (mirrors
        CandidateRepository.get_by_ids' convention). A resume reused across
        campaigns (via "use existing" duplicate resolution) can have more
        than one row here per resume_id; pass campaign_id to disambiguate
        to the single row for that specific campaign.
        """
        if not resume_ids:
            return []
        stmt = select(CampaignCandidate).where(CampaignCandidate.resume_id.in_(resume_ids))
        if campaign_id is not None:
            stmt = stmt.where(CampaignCandidate.campaign_id == campaign_id)
        return list(self.db.execute(stmt).scalars().all())

    def get_by_campaign_and_candidate(
        self,
        campaign_id: UUID,
        candidate_id: UUID,
    ) -> CampaignCandidate | None:
        """
        Returns campaign candidate if already exists.
        """

        return (
            self.db.query(CampaignCandidate)
            .filter(
                CampaignCandidate.campaign_id == campaign_id,
                CampaignCandidate.candidate_id == candidate_id,
            )
            .first()
        )

    def get_campaign_context_for_candidate(
        self,
        candidate_id: UUID,
    ) -> list[tuple[str, PipelineStage]]:
        """
        Epic 3 (M05-E03) Phase C2 — minimal (campaign_name, pipeline_stage)
        pairs for a candidate, most recent first, backing the duplicate-file
        warning's campaign_names + current_pipeline_stage fields only. Not
        C6's full cross-campaign tracking view (score/outcome/etc.) — that's
        this same join with more columns, left for C6 to build when it
        actually needs them, so this stays a narrow, single-purpose query.
        """
        stmt = (
            select(HiringCampaign.name, CampaignCandidate.pipeline_stage)
            .join(HiringCampaign, HiringCampaign.id == CampaignCandidate.campaign_id)
            .where(CampaignCandidate.candidate_id == candidate_id)
            .order_by(CampaignCandidate.created_at.desc())
        )
        return list(self.db.execute(stmt).all())

    def get_all_by_candidate_across_campaigns(
        self,
        candidate_id: UUID,
    ) -> list[tuple[CampaignCandidate, str, str | None]]:
        """
        Epic 3 (M05-E03) Phase C6 — the full cross-campaign history join
        get_campaign_context_for_candidate's docstring earmarks for this
        phase: (campaign_candidate, campaign_name, jd_title) rows, most
        recent first. Secondary sort by id keeps ordering deterministic
        when two rows share the same created_at timestamp.
        """
        stmt = (
            select(CampaignCandidate, HiringCampaign.name, JobDescription.title)
            .join(HiringCampaign, HiringCampaign.id == CampaignCandidate.campaign_id)
            .join(JobDescription, JobDescription.id == HiringCampaign.jd_id)
            .where(CampaignCandidate.candidate_id == candidate_id)
            .order_by(CampaignCandidate.created_at.desc(), CampaignCandidate.id.desc())
        )
        return list(self.db.execute(stmt).all())

    def get_high_frequency_resubmissions(
        self,
        window_days: int,
        threshold: int,
    ) -> list[tuple[UUID, int]]:
        """
        Epic 3 (M05-E03) Phase C4 — (candidate_id, submission_count) for
        every candidate with at least `threshold` campaign_candidates rows
        created within the last `window_days`. Not scoped to ACTIVE
        campaigns — this is about the candidate's own submission behavior
        over time, not current campaign state.
        """
        since = datetime.now(timezone.utc) - timedelta(days=window_days)
        stmt = (
            select(CampaignCandidate.candidate_id, func.count())
            .where(CampaignCandidate.created_at >= since)
            .group_by(CampaignCandidate.candidate_id)
            .having(func.count() >= threshold)
        )
        return list(self.db.execute(stmt).all())

    def get_most_recent_campaign_for_candidate(self, candidate_id: UUID) -> HiringCampaign | None:
        """
        Epic 3 (M05-E03) Phase C4 — resolves the candidate's most recent
        campaign submission, for attributing the resubmission-alert audit
        event's actor_id to that campaign's created_by (mirroring
        CampaignSchedulerService._raise_health_alert's exact convention,
        since AuditLog.actor_id is a required, non-null FK and no synthetic
        SYSTEM actor exists in this codebase).
        """
        stmt = (
            select(HiringCampaign)
            .join(CampaignCandidate, CampaignCandidate.campaign_id == HiringCampaign.id)
            .where(CampaignCandidate.candidate_id == candidate_id)
            .order_by(CampaignCandidate.created_at.desc())
            .limit(1)
        )
        return self.db.execute(stmt).scalars().first()

    def reset_for_resubmission(
        self,
        campaign_candidate: CampaignCandidate,
        new_resume_id: UUID,
    ) -> CampaignCandidate:
        """
        Epic 3 (M05-E03) Phase C5 — points the campaign_candidate at the
        newly-uploaded resume and clears every evaluation-derived field
        (scoring, AI, fraud, rejection, HR override) so the candidate is
        scored from scratch. Identity/relationship fields (campaign_id,
        candidate_id, idempotency_key), created_at, and recruiter_notes are
        left untouched. Does not touch pipeline_stage — the caller is
        expected to have already moved that via PipelineTransitionService.
        """
        campaign_candidate.resume_id = new_resume_id
        campaign_candidate.screened_at = None
        campaign_candidate.deterministic_score = None
        campaign_candidate.deterministic_passed = None
        campaign_candidate.score_breakdown = None
        campaign_candidate.semantic_score = None
        campaign_candidate.ai_ats_score = None
        campaign_candidate.ai_confidence = None
        campaign_candidate.effective_ai_score = None
        campaign_candidate.ai_recommendation = None
        campaign_candidate.ai_strengths = None
        campaign_candidate.ai_weaknesses = None
        campaign_candidate.ai_evaluation_status = AIEvaluationStatus.PENDING
        campaign_candidate.ai_retry_count = 0
        campaign_candidate.composite_score = None
        campaign_candidate.fraud_flags = None
        campaign_candidate.is_fraud_flagged = False
        campaign_candidate.rejection_reason = None
        campaign_candidate.rejection_layer = None
        campaign_candidate.hr_override = False
        campaign_candidate.hr_override_by = None
        campaign_candidate.hr_override_reason = None
        campaign_candidate.hr_override_at = None

        self.db.flush()
        self.db.refresh(campaign_candidate)
        return campaign_candidate

    def get_candidate_count(
        self,
        campaign_id: UUID,
    ) -> int:
        """
        Returns total number of candidates in a campaign.
        """
        return (
            self.db.query(func.count(CampaignCandidate.id))
            .filter(CampaignCandidate.campaign_id == campaign_id)
            .scalar()
            or 0
        )

    def get_rejected_by_campaign(
        self,
        campaign_id: UUID,
    ) -> list[CampaignCandidate]:
        """
        M07-E03 S03 T03: every REJECTED campaign_candidate in a campaign,
        for export. Filters at the DB level (same campaign-scoping
        convention as get_all_by_campaign) rather than fetching every
        candidate and filtering in Python.
        """
        return (
            self.db.query(CampaignCandidate)
            .filter(
                CampaignCandidate.campaign_id == campaign_id,
                CampaignCandidate.pipeline_stage == PipelineStage.REJECTED,
            )
            .all()
        )

    def get_overridden(
        self,
        campaign_id: UUID | None = None,
        date_from=None,
        date_to=None,
    ) -> list[CampaignCandidate]:
        """
        M07-E03 S04 T03: every campaign_candidate with hr_override=True,
        optionally scoped to one campaign and/or an hr_override_at date
        range - backs the Override Report's rows, weekly trend and
        per-campaign alert, all of which call this with different filter
        combinations rather than each running their own query.
        """
        stmt = select(CampaignCandidate).where(CampaignCandidate.hr_override.is_(True))
        if campaign_id is not None:
            stmt = stmt.where(CampaignCandidate.campaign_id == campaign_id)
        if date_from is not None:
            stmt = stmt.where(CampaignCandidate.hr_override_at >= date_from)
        if date_to is not None:
            stmt = stmt.where(CampaignCandidate.hr_override_at <= date_to)
        stmt = stmt.order_by(CampaignCandidate.hr_override_at.desc())
        return self.db.execute(stmt).scalars().all()

    def get_all_by_campaign(
        self,
        campaign_id: UUID,
    ):
        """
        Returns all candidates belonging to a campaign, joined with their
        Candidate and Resume rows for the Candidate Listing page (candidate
        name, parsed designation/experience) - LEFT JOINed so a row is never
        dropped even if a candidate/resume were ever missing (both FKs are
        NOT NULL today; this is defensive, not expected to matter). No
        scores are computed here - deterministic_score/ai_ats_score/
        semantic_score/composite_score are read directly off
        CampaignCandidate exactly as already stored by the scoring
        pipeline. Returns a list of (CampaignCandidate, Candidate, Resume)
        rows.
        """
        stmt = (
            select(CampaignCandidate, Candidate, Resume)
            .outerjoin(Candidate, CampaignCandidate.candidate_id == Candidate.id)
            .outerjoin(Resume, CampaignCandidate.resume_id == Resume.id)
            .where(CampaignCandidate.campaign_id == campaign_id)
            .order_by(CampaignCandidate.created_at.desc())
        )

        return self.db.execute(stmt).all()

    def update(
        self,
        campaign_candidate: CampaignCandidate,
    ) -> CampaignCandidate:
        """
        Update campaign candidate.
        """
        self.db.flush()
        self.db.refresh(campaign_candidate)
        return campaign_candidate

    def delete(
        self,
        campaign_candidate: CampaignCandidate,
    ) -> None:
        """
        Delete campaign candidate.
        """
        self.db.delete(campaign_candidate)
        self.db.flush()

    def commit(self) -> None:
        """
        Commit transaction.
        """
        self.db.commit()

    def rollback(self) -> None:
        """
        Rollback transaction.
        """
        self.db.rollback()