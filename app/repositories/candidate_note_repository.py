from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.candidate_notes import CandidateNote


class CandidateNoteRepository:
    """M11-E04-S01 — recruiter notes. Soft-deleted rows are never returned."""

    def __init__(self, db: Session):
        self.db = db

    def list_for_candidate(self, campaign_candidate_id: UUID) -> list[CandidateNote]:
        return (
            self.db.query(CandidateNote)
            .filter(
                CandidateNote.campaign_candidate_id == campaign_candidate_id,
                CandidateNote.deleted_at.is_(None),
            )
            .order_by(CandidateNote.created_at.desc())
            .all()
        )

    def get_by_id(self, note_id: UUID) -> CandidateNote | None:
        return (
            self.db.query(CandidateNote)
            .filter(CandidateNote.id == note_id, CandidateNote.deleted_at.is_(None))
            .first()
        )

    def counts_for_candidates(self, campaign_candidate_ids: list[UUID]) -> dict[str, int]:
        """
        T03's count badge for a whole page of candidates in one query — the
        alternative is one COUNT per row, which is what makes list pages slow.
        """
        if not campaign_candidate_ids:
            return {}
        rows = self.db.execute(
            select(
                CandidateNote.campaign_candidate_id,
                func.count(CandidateNote.id).label("note_count"),
            )
            .where(
                CandidateNote.campaign_candidate_id.in_(campaign_candidate_ids),
                CandidateNote.deleted_at.is_(None),
            )
            .group_by(CandidateNote.campaign_candidate_id)
        ).all()
        return {str(r.campaign_candidate_id): r.note_count for r in rows}

    def add(self, note: CandidateNote) -> CandidateNote:
        self.db.add(note)
        self.db.flush()
        self.db.refresh(note)
        return note

    def commit(self) -> None:
        self.db.commit()

    def rollback(self) -> None:
        self.db.rollback()
