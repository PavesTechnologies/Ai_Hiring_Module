from uuid import UUID

from sqlalchemy.orm import Session

from app.models.pipeline import CandidateRejection


class CandidateRejectionRepository:

    def __init__(self, db: Session):
        self.db = db

    def create(self, rejection: CandidateRejection) -> CandidateRejection:
        self.db.add(rejection)
        self.db.flush()
        self.db.refresh(rejection)
        return rejection

    def get_by_campaign_candidate_id(self, campaign_candidate_id: UUID) -> list[CandidateRejection]:
        return (
            self.db.query(CandidateRejection)
            .filter(CandidateRejection.campaign_candidate_id == campaign_candidate_id)
            .order_by(CandidateRejection.rejected_at.desc())
            .all()
        )

    def commit(self) -> None:
        self.db.commit()

    def rollback(self) -> None:
        self.db.rollback()
