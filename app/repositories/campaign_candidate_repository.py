from datetime import datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy import and_, case, delete, func, nullslast, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, contains_eager, joinedload, lazyload

from app.models.campaigns import HiringCampaign
from app.models.candidates import Candidate, Resume
from app.models.embeddings import ResumeEmbedding
from app.models.jd.job_descriptions import JobDescription
from app.models.pipeline import (
    AIEvaluationStatus,
    AIRecommendation,
    CampaignCandidate,
    CampaignCandidateAIEvaluation,
    CampaignCandidateStageHistory,
    DecisionType,
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

    def get_by_id_for_update(
        self,
        campaign_candidate_id: UUID,
    ) -> CampaignCandidate | None:
        """
        E02: locking read (SELECT ... FOR UPDATE) so a concurrent transition
        on the same candidate is serialized instead of racing - the lock is
        released when the caller commits/rolls back (same convention as
        CampaignRepository.get_by_id_for_update).

        Explicitly overrides ai_evaluation off its default joinedload
        (see get_by_id above) with lazyload - ai_evaluation is a nullable
        1:1, so leaving the eager join in place here would hit the exact
        same Postgres rejection CampaignRepository.get_by_id_for_update
        already had to work around: "FOR UPDATE cannot be applied to the
        nullable side of an outer join".
        """
        return (
            self.db.query(CampaignCandidate)
            .options(lazyload(CampaignCandidate.ai_evaluation))
            .filter(CampaignCandidate.id == campaign_candidate_id)
            .with_for_update()
            .first()
        )

    def get_stage_history_by_idempotency_key(
        self,
        idempotency_key: str,
    ) -> CampaignCandidateStageHistory | None:
        return (
            self.db.query(CampaignCandidateStageHistory)
            .filter(CampaignCandidateStageHistory.idempotency_key == idempotency_key)
            .first()
        )

    def create_stage_history_idempotent(
        self,
        campaign_candidate_id: UUID,
        to_stage: PipelineStage,
        from_stage: PipelineStage | None = None,
        changed_by: str | None = None,
        change_reason: str | None = None,
        transition_source: TransitionSource = TransitionSource.SYSTEM,
        scores_snapshot: dict | None = None,
        idempotency_key: str | None = None,
    ) -> tuple[CampaignCandidateStageHistory, bool]:
        """
        E02: idempotency_key counterpart to create_stage_history - same
        SAVEPOINT + IntegrityError-catch shape as create_idempotent above.
        Does NOT touch create_stage_history itself or either of its two
        live callers (transition_to_rejected/apply_hr_override) - this is a
        separate method for StageTransitionService.transition() only.

        idempotency_key is nullable and only uniquely indexed WHERE NOT
        NULL (see migration 08655d0b0117) - passing None here means "no
        replay protection for this call", not an error; every NULL-keyed
        insert always succeeds since NULLs never collide against each other
        in that partial index.
        """
        history = CampaignCandidateStageHistory(
            campaign_candidate_id=campaign_candidate_id,
            from_stage=from_stage,
            to_stage=to_stage,
            changed_by=changed_by,
            change_reason=change_reason,
            transition_source=transition_source,
            scores_snapshot=scores_snapshot,
            idempotency_key=idempotency_key,
        )
        try:
            with self.db.begin_nested():
                self.db.add(history)
                self.db.flush()
            self.db.refresh(history)
            return history, True
        except IntegrityError:
            existing = self.get_stage_history_by_idempotency_key(idempotency_key)
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

    def get_stage_history_by_campaign_candidate_id(
        self,
        campaign_candidate_id: UUID,
    ) -> list[CampaignCandidateStageHistory]:
        """
        M10-E03 Phase 2: the complete pipeline-stage transition history for
        ONE candidate, oldest first - backs the Candidate Timeline API.
        Distinct from CampaignRepository.get_stage_history(campaign_id),
        which returns every candidate's stage history for an entire
        campaign (used by the campaign-wide activity timeline) - that
        method is reused as-is and is not touched here; this is simply the
        single-candidate-scoped counterpart the campaign-wide one never
        needed. Ordered ascending (oldest first), matching this service's
        existing "Timeline" convention (_build_processing_timeline's own
        "oldest first" ordering for the Processing Timeline), as opposed to
        the "most recent first" convention used by *_history-named reads
        elsewhere (get_overridden, CandidateCompositeScoreHistoryRepository).
        """
        stmt = (
            select(CampaignCandidateStageHistory)
            .where(CampaignCandidateStageHistory.campaign_candidate_id == campaign_candidate_id)
            .order_by(CampaignCandidateStageHistory.changed_at.asc())
        )
        return list(self.db.execute(stmt).scalars().all())

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
        Get campaign candidate by ID. Eager-loads ai_evaluation (one cheap
        join on an indexed unique FK) since nearly every caller that fetches
        a single candidate by id also needs its AI evaluation fields
        (composite scoring, scorecard/summary responses, HR override) -
        centralizing the eager load here avoids scattering .options() calls
        and avoids an implicit per-call lazy load.
        """
        return (
            self.db.query(CampaignCandidate)
            .options(joinedload(CampaignCandidate.ai_evaluation))
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

    def get_campaign_usage_by_resume_ids(
        self,
        resume_ids: list[UUID],
    ) -> list[tuple[UUID, UUID, str, PipelineStage]]:
        """
        S02-T01 - (resume_id, campaign_id, campaign_name, pipeline_stage) rows
        for a batch of resume versions, backing "which campaigns was this
        resume version used in". Mirrors get_campaign_context_for_candidate's
        join but keyed by resume_id (a resume can be linked to more than one
        campaign via "use existing" duplicate resolution) and batched like
        get_by_resume_ids.
        """
        if not resume_ids:
            return []
        stmt = (
            select(
                CampaignCandidate.resume_id,
                HiringCampaign.id,
                HiringCampaign.name,
                CampaignCandidate.pipeline_stage,
            )
            .join(HiringCampaign, HiringCampaign.id == CampaignCandidate.campaign_id)
            .where(CampaignCandidate.resume_id.in_(resume_ids))
            .order_by(CampaignCandidate.created_at.desc())
        )
        return list(self.db.execute(stmt).all())

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

    def get_existing_candidate_ids(
        self,
        campaign_id: UUID,
        candidate_ids: list[UUID],
    ) -> set[UUID]:
        """
        Batch dedup check for bulk candidate addition - which of
        candidate_ids already have a campaign_candidates row for this
        campaign, in one query instead of one get_by_campaign_and_candidate
        call per candidate.
        """
        if not candidate_ids:
            return set()
        rows = (
            self.db.query(CampaignCandidate.candidate_id)
            .filter(
                CampaignCandidate.campaign_id == campaign_id,
                CampaignCandidate.candidate_id.in_(candidate_ids),
            )
            .all()
        )
        return {row.candidate_id for row in rows}

    def bulk_create(
        self,
        campaign_candidates: list[CampaignCandidate],
    ) -> list[CampaignCandidate]:
        """
        Inserts many new campaign_candidates rows in one flush instead of
        one create_idempotent (each its own SAVEPOINT) per row - for bulk
        candidate addition, where dedup against existing rows already
        happened via get_existing_candidate_ids, so no per-row conflict
        handling is needed here.
        """
        if not campaign_candidates:
            return []
        self.db.add_all(campaign_candidates)
        self.db.flush()
        for campaign_candidate in campaign_candidates:
            self.db.refresh(campaign_candidate)
        return campaign_candidates

    def bulk_create_stage_history(
        self,
        histories: list[CampaignCandidateStageHistory],
    ) -> list[CampaignCandidateStageHistory]:
        """Bulk counterpart to create_stage_history - one flush for the whole batch."""
        if not histories:
            return []
        self.db.add_all(histories)
        self.db.flush()
        return histories

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
        campaign submission, so ResubmissionAlertService.evaluate_resubmission_alerts
        can attach a campaign_id to the CAMPAIGN_RESUBMISSION_DETECTED audit
        entry. Previously also used to attribute actor_id to that campaign's
        created_by - removed in Epic 3 Fix 2 (actor_id is nullable; this is
        a scheduled sweep, not that user's action - see
        ResubmissionAlertService's docstring).
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
        (scoring, fraud, decision) so the candidate is scored from
        scratch. Identity/relationship fields (campaign_id, candidate_id,
        idempotency_key) and created_at are left untouched. Does not touch
        pipeline_stage — the caller is expected to have already moved that
        via PipelineTransitionService. AI evaluation fields live on the
        related CampaignCandidateAIEvaluation row now — reset separately by
        the caller via CampaignCandidateAIEvaluationRepository.reset().

        Preserves the pre-existing asymmetry where semantic_breakdown is
        NOT reset even though semantic_score is (score_breakdown/
        deterministic_breakdown IS reset alongside deterministic_score) —
        not fixed here, out of scope for this refactor.
        """
        campaign_candidate.resume_id = new_resume_id
        self._reset_evaluation_derived_fields(campaign_candidate)

        self.db.flush()
        self.db.refresh(campaign_candidate)
        return campaign_candidate

    def _reset_evaluation_derived_fields(self, campaign_candidate: CampaignCandidate) -> None:
        """Shared by reset_for_resubmission (new resume) and reset_for_rescore (same resume, manual re-score trigger) - every evaluation-derived field, nothing identity/relationship-related."""
        campaign_candidate.screened_at = None
        campaign_candidate.deterministic_score = None
        campaign_candidate.deterministic_passed = None
        campaign_candidate.deterministic_breakdown = None
        campaign_candidate.semantic_score = None
        campaign_candidate.semantic_passed = None
        campaign_candidate.composite_score = None
        campaign_candidate.composite_score_computed_at = None
        campaign_candidate.fraud_flags = None
        campaign_candidate.is_fraud_flagged = False
        campaign_candidate.decision_type = None
        campaign_candidate.decision_source = None
        campaign_candidate.decision_reason = None
        campaign_candidate.decision_details = None
        campaign_candidate.decision_by_user_id = None
        campaign_candidate.decision_at = None

    def reset_for_rescore(self, campaign_candidate: CampaignCandidate) -> CampaignCandidate:
        """
        Epic 5 follow-up - manual re-score trigger (moving a candidate to
        SCREENING from anywhere other than UPLOADED). Same field list as
        reset_for_resubmission, EXCEPT resume_id - there's no new resume
        here, the same one is being re-scored, so pointing at a
        different resume would be wrong. AI evaluation fields still live
        on the separate CampaignCandidateAIEvaluation row - reset
        separately by the caller via CampaignCandidateAIEvaluationRepository.reset().

        Unlike reset_for_resubmission, also clears semantic_breakdown and
        semantic_score_computed_at - found live, via a candidate re-scored
        from FRAUD_REVIEW showing a stale PASSED breakdown from its prior
        scoring run (semantic_score itself was NULL, but semantic_breakdown
        JSON survives reset_for_resubmission's pre-existing gap) alongside a
        semantic scoring re-trigger that never actually dispatched. Not
        applied to reset_for_resubmission - that path's own copy of this gap
        is untouched, out of scope for this fix.
        """
        self._reset_evaluation_derived_fields(campaign_candidate)
        campaign_candidate.semantic_breakdown = None
        campaign_candidate.semantic_score_computed_at = None

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

    def count_rejected_by_campaigns(self, campaign_ids) -> dict:
        """
        {campaign_id: rejected_count} for many campaigns in one aggregate query.

        Callers that only need the number must use this rather than
        len(get_rejected_by_campaign(id)) per campaign, which materialises every
        rejected row just to discard it.
        """
        ids = [cid for cid in set(campaign_ids or []) if cid]
        if not ids:
            return {}
        rows = (
            self.db.query(
                CampaignCandidate.campaign_id,
                func.count(CampaignCandidate.id),
            )
            .filter(
                CampaignCandidate.campaign_id.in_(ids),
                CampaignCandidate.pipeline_stage == PipelineStage.REJECTED,
            )
            .group_by(CampaignCandidate.campaign_id)
            .all()
        )
        return {cid: count for cid, count in rows}

    def get_overridden(
        self,
        campaign_id: UUID | None = None,
        date_from=None,
        date_to=None,
    ) -> list[CampaignCandidate]:
        """
        M07-E03 S04 T03: every campaign_candidate currently in the RESET
        (HR override) decision state, optionally scoped to one campaign
        and/or a decision_at date range - backs the Override Report's
        rows, weekly trend and per-campaign alert, all of which call this
        with different filter combinations rather than each running their
        own query.
        """
        stmt = select(CampaignCandidate).where(CampaignCandidate.decision_type == DecisionType.RESET)
        if campaign_id is not None:
            stmt = stmt.where(CampaignCandidate.campaign_id == campaign_id)
        if date_from is not None:
            stmt = stmt.where(CampaignCandidate.decision_at >= date_from)
        if date_to is not None:
            stmt = stmt.where(CampaignCandidate.decision_at <= date_to)
        stmt = stmt.order_by(CampaignCandidate.decision_at.desc())
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
        scores are computed here - deterministic_score/semantic_score/
        composite_score/ai_evaluation are read directly off CampaignCandidate
        exactly as already stored by the scoring pipeline. ai_evaluation is
        eager-loaded to avoid N+1 across the page. Returns a list of
        (CampaignCandidate, Candidate, Resume) rows.
        """
        stmt = (
            select(CampaignCandidate, Candidate, Resume)
            .outerjoin(Candidate, CampaignCandidate.candidate_id == Candidate.id)
            .outerjoin(Resume, CampaignCandidate.resume_id == Resume.id)
            .options(joinedload(CampaignCandidate.ai_evaluation))
            .where(CampaignCandidate.campaign_id == campaign_id)
            .order_by(CampaignCandidate.created_at.desc())
        )

        return self.db.execute(stmt).unique().all()

    def get_candidate_ids_by_campaign(self, campaign_id: UUID) -> set[UUID]:
        """
        Bare candidate_ids already added to a campaign - used by Talent
        Pool search to exclude already-added candidates when an HR_ADMIN is
        browsing to add people to a specific campaign. Deliberately not
        get_all_by_campaign's full Candidate/Resume/ai_evaluation join -
        only the id set is needed here.
        """
        stmt = select(CampaignCandidate.candidate_id).where(CampaignCandidate.campaign_id == campaign_id)
        return set(self.db.execute(stmt).scalars().all())

    def get_ids_by_campaign(
        self,
        campaign_id: UUID,
    ) -> list[UUID]:
        """
        M10-E01: bare campaign_candidate ids for a campaign, used solely to
        fan out composite-score recalculation after a campaign's scoring
        weights change - deliberately not the full joined
        get_all_by_campaign query (Candidate/Resume are irrelevant to
        recalculation and would be wasted work at fan-out scale).
        """
        stmt = select(CampaignCandidate.id).where(CampaignCandidate.campaign_id == campaign_id)
        return list(self.db.execute(stmt).scalars().all())

    # M10-E03 Phase 1: sort_by -> the actual column it maps to. "ai_score"
    # maps to effective_ai_score (the score CompositeScoringService itself
    # reads), now on the related CampaignCandidateAIEvaluation row rather
    # than a raw column here - the same column the composite formula
    # already treats as authoritative.
    _RANKING_SORT_COLUMNS = {
        "composite_score": CampaignCandidate.composite_score,
        "deterministic_score": CampaignCandidate.deterministic_score,
        "semantic_score": CampaignCandidate.semantic_score,
        "ai_score": CampaignCandidateAIEvaluation.effective_ai_score,
        "created_at": CampaignCandidate.created_at,
    }

    def get_ranked_by_campaign(
        self,
        campaign_id: UUID,
        page: int,
        page_size: int,
        sort_by: str | None = None,
        sort_order: str = "desc",
        pipeline_stage: PipelineStage | None = None,
        composite_score_min: float | None = None,
        composite_score_max: float | None = None,
        ai_recommendation: AIRecommendation | None = None,
        ai_evaluation_status: AIEvaluationStatus | None = None,
        include_pending: bool = True,
        include_rejected: bool = True,
        include_fraud: bool = True,
        hr_override: bool | None = None,
    ) -> tuple[list, int]:
        """
        M10-E03 Phase 1: the ranked candidate list's core query - one
        filtered/sorted/paginated SELECT (joined with Candidate/Resume/
        CampaignCandidateAIEvaluation, same LEFT JOIN shape as
        get_all_by_campaign) plus one COUNT(*) with the identical filter
        set, so the caller can build page metadata without a second round
        trip through Python. All filters combine with AND. Ranking is
        always performed by PostgreSQL - ORDER BY is built here and nowhere
        does this method (or any caller) re-sort in Python.

        Default ordering (sort_by=None): composite_score DESC NULLS LAST,
        deterministic_score DESC, created_at ASC, id ASC - so unscored
        ("pending") candidates are never ranked ahead of scored ones.
        Explicit sort_by: the chosen column (NULLS LAST regardless of
        direction, for the same reason), then created_at ASC, id ASC -
        every ordering ends in those two columns so ties (identical scores,
        identical timestamps) still produce a total, stable order across
        pages.

        include_pending=False excludes any candidate with composite_score
        IS NULL entirely (covers both the PENDING and FAILED
        ranking_status, since both only exist when composite_score is
        NULL) - see CampaignCandidateService._derive_ranking_status for how
        those two are distinguished for display.

        AI filters (ai_recommendation/ai_evaluation_status) are evaluated
        against the outer-joined CampaignCandidateAIEvaluation row - a
        candidate with no AI evaluation row yet has every ai_* column NULL
        via the join, which correctly never matches ai_recommendation
        (never a valid filter value) and is treated as PENDING for
        ai_evaluation_status (matching the pre-split default), not excluded
        outright.
        """
        filters = [CampaignCandidate.campaign_id == campaign_id]
        if pipeline_stage is not None:
            filters.append(CampaignCandidate.pipeline_stage == pipeline_stage)
        if composite_score_min is not None:
            filters.append(CampaignCandidate.composite_score >= composite_score_min)
        if composite_score_max is not None:
            filters.append(CampaignCandidate.composite_score <= composite_score_max)
        if ai_recommendation is not None:
            filters.append(CampaignCandidateAIEvaluation.ai_recommendation == ai_recommendation)
        if ai_evaluation_status is not None:
            if ai_evaluation_status == AIEvaluationStatus.PENDING:
                filters.append(or_(
                    CampaignCandidateAIEvaluation.id.is_(None),
                    CampaignCandidateAIEvaluation.ai_evaluation_status == AIEvaluationStatus.PENDING,
                ))
            else:
                filters.append(CampaignCandidateAIEvaluation.ai_evaluation_status == ai_evaluation_status)
        if not include_pending:
            filters.append(CampaignCandidate.composite_score.is_not(None))
        if not include_rejected:
            filters.append(CampaignCandidate.pipeline_stage != PipelineStage.REJECTED)
        if not include_fraud:
            filters.append(CampaignCandidate.is_fraud_flagged.is_(False))
        if hr_override is not None:
            if hr_override:
                filters.append(CampaignCandidate.decision_type == DecisionType.RESET)
            else:
                filters.append(CampaignCandidate.decision_type.is_distinct_from(DecisionType.RESET))

        total = self.db.execute(
            select(func.count())
            .select_from(CampaignCandidate)
            .outerjoin(CampaignCandidateAIEvaluation, CampaignCandidateAIEvaluation.campaign_candidate_id == CampaignCandidate.id)
            .where(*filters)
        ).scalar() or 0

        stmt = (
            select(CampaignCandidate, Candidate, Resume)
            .outerjoin(Candidate, CampaignCandidate.candidate_id == Candidate.id)
            .outerjoin(Resume, CampaignCandidate.resume_id == Resume.id)
            .outerjoin(CampaignCandidateAIEvaluation, CampaignCandidateAIEvaluation.campaign_candidate_id == CampaignCandidate.id)
            .options(contains_eager(CampaignCandidate.ai_evaluation))
            .where(*filters)
        )

        if sort_by is None:
            stmt = stmt.order_by(
                nullslast(CampaignCandidate.composite_score.desc()),
                CampaignCandidate.deterministic_score.desc(),
                CampaignCandidate.created_at.asc(),
                CampaignCandidate.id.asc(),
            )
        else:
            column = self._RANKING_SORT_COLUMNS[sort_by]
            ordered_column = column.asc() if sort_order == "asc" else column.desc()
            stmt = stmt.order_by(
                nullslast(ordered_column),
                CampaignCandidate.created_at.asc(),
                CampaignCandidate.id.asc(),
            )

        stmt = stmt.limit(page_size).offset((page - 1) * page_size)

        return self.db.execute(stmt).unique().all(), total

    def get_score_aggregates(self, campaign_id: UUID) -> dict:
        """
        M10-E03 Phase 1: one query, five aggregates - total/ranked/pending-
        vs-failed split/rejected/fraud counts plus highest/lowest/average
        composite_score - backing the ranking summary endpoint. ranked =
        COUNT(composite_score) (COUNT on a column ignores NULLs, so this is
        exactly "candidates with a composite score" with no CASE needed).
        failed is the only split requiring a CASE: composite_score IS NULL
        AND ai_evaluation_status = FAILED - the same distinction
        CampaignCandidateService._derive_ranking_status draws per-row.
        SUM(...)/MAX/MIN/AVG all return NULL (not 0) over zero matching
        rows - guarded with `or 0`/`is not None` below so an empty campaign
        returns 0 counts and None scores rather than raising.
        """
        stmt = select(
            func.count(CampaignCandidate.id),
            func.count(CampaignCandidate.composite_score),
            func.sum(case(
                (and_(
                    CampaignCandidate.composite_score.is_(None),
                    CampaignCandidateAIEvaluation.ai_evaluation_status == AIEvaluationStatus.FAILED,
                ), 1),
                else_=0,
            )),
            func.sum(case((CampaignCandidate.pipeline_stage == PipelineStage.REJECTED, 1), else_=0)),
            func.sum(case((CampaignCandidate.is_fraud_flagged.is_(True), 1), else_=0)),
            func.max(CampaignCandidate.composite_score),
            func.min(CampaignCandidate.composite_score),
            func.avg(CampaignCandidate.composite_score),
        ).outerjoin(
            CampaignCandidateAIEvaluation, CampaignCandidateAIEvaluation.campaign_candidate_id == CampaignCandidate.id,
        ).where(CampaignCandidate.campaign_id == campaign_id)

        total, ranked, failed, rejected, fraud, highest, lowest, average = self.db.execute(stmt).one()

        return {
            "total": total or 0,
            "ranked": ranked or 0,
            "failed": failed or 0,
            "rejected": rejected or 0,
            "fraud": fraud or 0,
            "highest": float(highest) if highest is not None else None,
            "lowest": float(lowest) if lowest is not None else None,
            "average": float(average) if average is not None else None,
        }

    def get_best_composite_scores_by_candidate_ids(self, candidate_ids: list[UUID]) -> dict[UUID, float]:
        """
        Talent Pool Search (M13-E01 S02 T0x) - MAX(composite_score) per
        candidate for a batch of candidates in ONE query, so the Talent
        Pool list page's "best historical composite score" card field never
        issues one aggregate query per candidate. Mirrors
        get_score_aggregates' MAX(composite_score), generalized across every
        campaign a candidate has ever been submitted to (not one campaign)
        and grouped per candidate instead of scalar for one campaign.

        A candidate with campaign_candidates rows but every composite_score
        NULL (not yet scored) groups to a NULL MAX and is filtered out below
        - simply absent from the returned dict, exactly like a candidate
        with no campaign_candidates rows at all. Callers must treat a
        missing key as "no score", never as 0.
        """
        if not candidate_ids:
            return {}
        stmt = (
            select(CampaignCandidate.candidate_id, func.max(CampaignCandidate.composite_score))
            .where(CampaignCandidate.candidate_id.in_(candidate_ids))
            .group_by(CampaignCandidate.candidate_id)
        )
        return {
            candidate_id: float(best_score)
            for candidate_id, best_score in self.db.execute(stmt).all()
            if best_score is not None
        }

    def get_ai_recommendation_counts(self, campaign_id: UUID) -> dict[str, int]:
        """
        M10-E03 Phase 1: AI recommendation breakdown for the ranking
        summary - mirrors CampaignRepository.get_stage_counts' exact
        GROUP BY shape for the analogous pipeline_stage breakdown, applied
        to ai_recommendation instead. NULL (not yet AI-evaluated) is
        excluded - it isn't a recommendation value, and total_candidates/
        pending_candidates already account for those rows.
        """
        stmt = (
            select(CampaignCandidateAIEvaluation.ai_recommendation, func.count())
            .join(CampaignCandidate, CampaignCandidateAIEvaluation.campaign_candidate_id == CampaignCandidate.id)
            .where(
                CampaignCandidate.campaign_id == campaign_id,
                CampaignCandidateAIEvaluation.ai_recommendation.is_not(None),
            )
            .group_by(CampaignCandidateAIEvaluation.ai_recommendation)
        )
        return {recommendation.value: count for recommendation, count in self.db.execute(stmt).all()}

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

    def get_pending_semantic_score_with_ready_embedding(self) -> list[CampaignCandidate]:
        """
        Automatic recovery (M08-E02): every campaign_candidate that already
        passed deterministic screening but has no semantic_score yet, AND
        whose resume now has an embedding - i.e. exactly the candidates
        calculate_semantic_score_task's own missing-resume-embedding skip
        path (see MISSING_RESUME_EMBEDDING_REASON) left behind, but whose
        embedding has since become available (a later EMBED_RESUME retry
        succeeded, or its earlier permanent failure was manually resolved).

        resume_embeddings has no FK/relationship to campaign_candidates or
        resumes (see ResumeRepository's own comments on this) - the join
        below is a manual EXISTS-style join condition on resume_id, the
        same pattern get_campaign_history_entries already uses to bridge
        the two tables.
        """
        return (
            self.db.query(CampaignCandidate)
            .join(ResumeEmbedding, ResumeEmbedding.resume_id == CampaignCandidate.resume_id)
            .filter(
                CampaignCandidate.deterministic_passed.is_(True),
                CampaignCandidate.semantic_score.is_(None),
            )
            .distinct()
            .all()
        )

    def get_screening_semantic_health_stats(self, campaign_id: UUID) -> tuple[int, int]:
        """
        Requirement 5 (embedding health monitoring): returns (affected_count,
        total_screening_count) for one campaign - affected_count is every
        SCREENING candidate with semantic_score IS NULL that hasn't already
        been triaged to MANUAL_REVIEW (i.e. silently stuck, not yet
        flagged); total_screening_count is every SCREENING candidate in
        the campaign, the percentage's denominator.
        """
        total_screening_count = (
            self.db.query(func.count(CampaignCandidate.id))
            .filter(
                CampaignCandidate.campaign_id == campaign_id,
                CampaignCandidate.pipeline_stage == PipelineStage.SCREENING,
            )
            .scalar()
        )
        affected_count = (
            self.db.query(func.count(CampaignCandidate.id))
            .outerjoin(
                CampaignCandidateAIEvaluation,
                CampaignCandidateAIEvaluation.campaign_candidate_id == CampaignCandidate.id,
            )
            .filter(
                CampaignCandidate.campaign_id == campaign_id,
                CampaignCandidate.pipeline_stage == PipelineStage.SCREENING,
                CampaignCandidate.semantic_score.is_(None),
                or_(
                    CampaignCandidateAIEvaluation.id.is_(None),
                    CampaignCandidateAIEvaluation.ai_evaluation_status != AIEvaluationStatus.MANUAL_REVIEW,
                ),
            )
            .scalar()
        )
        return affected_count or 0, total_screening_count or 0

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