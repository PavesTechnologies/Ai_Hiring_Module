from uuid import UUID

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.pipeline import AIEvaluationStatus, CampaignCandidateAIEvaluation


class CampaignCandidateAIEvaluationRepository:

    def __init__(self, db: Session):
        self.db = db

    def get_by_campaign_candidate_id(
        self,
        campaign_candidate_id: UUID,
    ) -> CampaignCandidateAIEvaluation | None:
        return (
            self.db.query(CampaignCandidateAIEvaluation)
            .filter(CampaignCandidateAIEvaluation.campaign_candidate_id == campaign_candidate_id)
            .first()
        )

    def get_or_create(self, campaign_candidate_id: UUID) -> CampaignCandidateAIEvaluation:
        """
        Every pre-existing campaign_candidate already has an AI evaluation
        row (migration backfill) - this only ever actually creates one for
        a candidate created after that migration. A SAVEPOINT scopes a
        concurrent-insert race to just this attempt, same pattern as
        CampaignCandidateRepository.create_idempotent.
        """
        existing = self.get_by_campaign_candidate_id(campaign_candidate_id)
        if existing is not None:
            return existing

        try:
            with self.db.begin_nested():
                evaluation = CampaignCandidateAIEvaluation(campaign_candidate_id=campaign_candidate_id)
                self.db.add(evaluation)
                self.db.flush()
            self.db.refresh(evaluation)
            return evaluation
        except IntegrityError:
            return self.get_by_campaign_candidate_id(campaign_candidate_id)

    def update(
        self,
        ai_evaluation: CampaignCandidateAIEvaluation,
    ) -> CampaignCandidateAIEvaluation:
        self.db.flush()
        self.db.refresh(ai_evaluation)
        return ai_evaluation

    def reset(self, ai_evaluation: CampaignCandidateAIEvaluation) -> CampaignCandidateAIEvaluation:
        """
        Resubmission (Epic 3 Phase C5): clears every AI evaluation field so
        the candidate is re-evaluated from scratch, same fields
        reset_for_resubmission used to null directly on campaign_candidates
        before the AI evaluation split - prompt_version_id is untouched,
        matching that method's original behavior.
        """
        ai_evaluation.ai_ats_score = None
        ai_evaluation.ai_confidence = None
        ai_evaluation.effective_ai_score = None
        ai_evaluation.ai_recommendation = None
        ai_evaluation.ai_strengths = None
        ai_evaluation.ai_weaknesses = None
        ai_evaluation.ai_response_json = None
        ai_evaluation.ai_evaluation_status = AIEvaluationStatus.PENDING
        ai_evaluation.ai_retry_count = 0
        self.db.flush()
        self.db.refresh(ai_evaluation)
        return ai_evaluation

    def commit(self) -> None:
        self.db.commit()

    def rollback(self) -> None:
        self.db.rollback()
