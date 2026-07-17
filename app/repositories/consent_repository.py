from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.compliance import CandidateConsent


class ConsentRepository:
    """
    CRUD for candidate_consent. Insert-only by design — the table is meant to
    be an immutable audit trail, so no update/delete methods are exposed here
    (mirrors the DB-level intent even though the app DB user isn't restricted
    at the grant level in this environment).
    """

    def __init__(self, db: Session):
        self.db = db

    def create(self, consent: CandidateConsent) -> CandidateConsent:
        self.db.add(consent)
        self.db.flush()
        self.db.refresh(consent)
        return consent

    def get_latest_by_candidate(self, candidate_id: UUID) -> CandidateConsent | None:
        stmt = (
            select(CandidateConsent)
            .where(CandidateConsent.candidate_id == candidate_id)
            .order_by(CandidateConsent.consent_timestamp.desc())
        )
        return self.db.execute(stmt).scalars().first()

    def commit(self) -> None:
        self.db.commit()

    def rollback(self) -> None:
        self.db.rollback()
