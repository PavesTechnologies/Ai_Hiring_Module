from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.candidates import Candidate, Resume
from app.models.pipeline import (
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