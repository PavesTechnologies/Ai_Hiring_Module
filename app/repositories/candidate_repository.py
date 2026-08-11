from datetime import datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.candidates import Candidate

_SORT_COLUMNS = {
    "created_at": Candidate.created_at,
}


class CandidateRepository:
    def __init__(self, db: Session):
        self.db = db

    def get_by_email_hash(self, email_hash: str) -> Candidate | None:
        stmt = select(Candidate).where(Candidate.email_hash == email_hash)
        return self.db.execute(stmt).scalars().first()

    def get_by_id(self, candidate_id: UUID) -> Candidate | None:
        return self.db.get(Candidate, candidate_id)

    def get_by_ids(self, candidate_ids: list[UUID]) -> list[Candidate]:
        """Batched counterpart to get_by_id — one query per page of a list endpoint, not one per row."""
        if not candidate_ids:
            return []
        stmt = select(Candidate).where(Candidate.id.in_(candidate_ids))
        return list(self.db.execute(stmt).scalars().all())

    def _build_search_conditions(self, email_hash: str | None, jurisdiction: str | None) -> list:
        """
        Global Candidates directory (GET /candidates) - full_name_encrypted/
        email_encrypted are encrypted at rest and can't be searched
        directly (same constraint ResumeRepository.search's email_hash
        filter already documents) - email_hash is the one exact-match
        identity lookup available; jurisdiction is a plain column.
        """
        conditions = []
        if email_hash is not None:
            conditions.append(Candidate.email_hash == email_hash)
        if jurisdiction is not None:
            conditions.append(Candidate.jurisdiction == jurisdiction)
        return conditions

    def search(
        self,
        *,
        email_hash: str | None = None,
        jurisdiction: str | None = None,
        page: int = 1,
        size: int = 20,
        sort_by: str = "created_at",
        sort_dir: str = "desc",
    ) -> list[Candidate]:
        """Global Candidates directory - every candidate regardless of campaign/Talent Pool membership. Read-only."""
        conditions = self._build_search_conditions(email_hash, jurisdiction)
        sort_column = _SORT_COLUMNS.get(sort_by, Candidate.created_at)
        order = sort_column.asc() if sort_dir == "asc" else sort_column.desc()

        stmt = (
            select(Candidate)
            .where(*conditions)
            .order_by(order)
            .offset((page - 1) * size)
            .limit(size)
        )
        return list(self.db.execute(stmt).scalars().all())

    def count_search(self, *, email_hash: str | None = None, jurisdiction: str | None = None) -> int:
        conditions = self._build_search_conditions(email_hash, jurisdiction)
        stmt = select(func.count()).select_from(Candidate).where(*conditions)
        return self.db.execute(stmt).scalar_one()

    def create(self, candidate: Candidate) -> tuple[Candidate, bool]:
        """
        Attempts to insert `candidate`. Two concurrent uploads for the same
        never-before-seen email can both see "no existing row" and both
        attempt an insert — email_hash is unique, so the loser's flush
        raises IntegrityError. A SAVEPOINT scopes that failure to just this
        insert attempt (mirrors SkillRepository.upsert_unknown_skill's
        pattern for the same class of race), then falls back to the row the
        winner just committed instead of raising.

        Returns (candidate, was_created).
        """
        try:
            with self.db.begin_nested():
                self.db.add(candidate)
                self.db.flush()
            self.db.refresh(candidate)
            return candidate, True
        except IntegrityError:
            existing = self.get_by_email_hash(candidate.email_hash)
            return existing, False

    def update_erasure_fields(
        self,
        candidate_id: UUID,
        erasure_requested_at: datetime | None = None,
        erasure_completed_at: datetime | None = None,
        is_pii_deleted: bool | None = None,
    ) -> Candidate | None:
        candidate = self.db.get(Candidate, candidate_id)
        if candidate is None:
            return None

        if erasure_requested_at is not None:
            candidate.erasure_requested_at = erasure_requested_at
        if erasure_completed_at is not None:
            candidate.erasure_completed_at = erasure_completed_at
        if is_pii_deleted is not None:
            candidate.is_pii_deleted = is_pii_deleted

        self.db.flush()
        return candidate

    def delete(self, candidate: Candidate) -> None:
        """Candidate erasure — hard-deletes the candidate row itself; caller must have already removed every child row referencing it."""
        self.db.delete(candidate)
        self.db.flush()

    def commit(self) -> None:
        self.db.commit()

    def rollback(self) -> None:
        self.db.rollback()
