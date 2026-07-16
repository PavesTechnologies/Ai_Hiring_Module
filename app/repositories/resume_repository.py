from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.candidates import ParseAttemptStatus, Resume, ResumeParseAttempt


class ResumeRepository:
    def __init__(self, db: Session):
        self.db = db

    def create(self, resume: Resume) -> Resume:
        self.db.add(resume)
        self.db.flush()
        self.db.refresh(resume)
        return resume

    def get_by_id(self, resume_id: UUID) -> Resume | None:
        return self.db.get(Resume, resume_id)

    def get_active_by_candidate(self, candidate_id: UUID) -> Resume | None:
        stmt = (
            select(Resume)
            .where(
                Resume.candidate_id == candidate_id,
                Resume.is_active_version.is_(True),
            )
            .order_by(Resume.version_number.desc())
        )
        return self.db.execute(stmt).scalars().first()

    def record_parse_attempt(
        self,
        resume_id: UUID,
        attempt_number: int,
        parser_used: str,
        status: ParseAttemptStatus,
        parser_version: str | None = None,
        ocr_used: bool = False,
        error_code: str | None = None,
        error_detail: str | None = None,
        confidence_score: float | None = None,
        duration_ms: int | None = None,
    ) -> ResumeParseAttempt:
        """
        No caller yet — actual parsing is Phase 8. Included now since the
        roadmap scoped this repository's full surface to this phase.
        """
        attempt = ResumeParseAttempt(
            resume_id=resume_id,
            attempt_number=attempt_number,
            parser_used=parser_used,
            parser_version=parser_version,
            ocr_used=ocr_used,
            status=status,
            error_code=error_code,
            error_detail=error_detail,
            confidence_score=confidence_score,
            duration_ms=duration_ms,
        )
        self.db.add(attempt)
        self.db.flush()
        self.db.refresh(attempt)
        return attempt

    def commit(self) -> None:
        self.db.commit()

    def rollback(self) -> None:
        self.db.rollback()
