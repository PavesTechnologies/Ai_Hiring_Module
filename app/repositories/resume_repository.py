from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.candidates import ParseAttemptStatus, ParseStatus, Resume, ResumeParseAttempt
from app.models.embeddings import EmbeddingModelVersion, ResumeEmbedding
from app.models.skills import CandidateSkill


class ResumeRepository:
    """
    CRUD for the Resume row plus the tables its per-file lifecycle touches:
    resume_parse_attempts (individual-upload retry logging), and the two
    tables the async pipeline's persistence stage writes to
    (resume_embeddings, candidate_skills). Mirrors JDRepository's shape for
    the Resume side.
    """

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

    def update_parsed_result(
        self,
        resume: Resume,
        parsed_json: dict,
        parse_status: ParseStatus,
        parser_version: str,
    ) -> Resume:
        resume.parsed_json = parsed_json
        resume.parse_status = parse_status
        resume.parser_version = parser_version
        self.db.flush()
        self.db.refresh(resume)
        return resume

    def mark_parse_failed(self, resume: Resume) -> Resume:
        resume.parse_status = ParseStatus.FAILED
        self.db.flush()
        self.db.refresh(resume)
        return resume

    def get_active_embedding_model_version(self) -> EmbeddingModelVersion:
        version = (
            self.db.query(EmbeddingModelVersion)
            .filter(EmbeddingModelVersion.is_active.is_(True))
            .first()
        )
        if not version:
            raise RuntimeError("No active embedding model version is configured.")
        return version

    def create_resume_embedding(
        self,
        resume_id: UUID,
        candidate_id: UUID,
        embedding: list[float],
        embedding_model_version_id: UUID,
        input_text_hash: str,
    ) -> ResumeEmbedding:
        resume_embedding = ResumeEmbedding(
            resume_id=resume_id,
            candidate_id=candidate_id,
            embedding=embedding,
            embedding_model_version_id=embedding_model_version_id,
            input_text_hash=input_text_hash,
        )
        self.db.add(resume_embedding)
        self.db.flush()
        self.db.refresh(resume_embedding)
        return resume_embedding

    def create_candidate_skill(
        self,
        candidate_id: UUID,
        resume_id: UUID,
        canonical_skill_id: UUID | None,
        raw_extracted_text: str,
        confidence: float | None,
        match_tier: str,
        status: str,
        scoring_weight: float = 1.0,
    ) -> CandidateSkill:
        candidate_skill = CandidateSkill(
            candidate_id=candidate_id,
            resume_id=resume_id,
            canonical_skill_id=canonical_skill_id,
            raw_extracted_text=raw_extracted_text,
            confidence=confidence,
            match_tier=match_tier,
            status=status,
            scoring_weight=scoring_weight,
        )
        self.db.add(candidate_skill)
        self.db.flush()
        self.db.refresh(candidate_skill)
        return candidate_skill

    def commit(self) -> None:
        self.db.commit()

    def rollback(self) -> None:
        self.db.rollback()
