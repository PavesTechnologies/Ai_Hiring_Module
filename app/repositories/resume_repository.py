from uuid import UUID

from sqlalchemy.orm import Session

from app.models.candidates import ParseStatus, Resume
from app.models.embeddings import EmbeddingModelVersion, ResumeEmbedding
from app.models.skills import CandidateSkill


class ResumeRepository:
    """
    CRUD for the Resume row itself plus the two tables its persistence
    stage writes to (resume_embeddings, candidate_skills). Mirrors
    JDRepository's shape for the Resume side.
    """

    def __init__(self, db: Session):
        self.db = db

    def get_by_id(self, resume_id: UUID) -> Resume | None:
        return self.db.query(Resume).filter(Resume.id == resume_id).first()

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
    ) -> CandidateSkill:
        candidate_skill = CandidateSkill(
            candidate_id=candidate_id,
            resume_id=resume_id,
            canonical_skill_id=canonical_skill_id,
            raw_extracted_text=raw_extracted_text,
            confidence=confidence,
            match_tier=match_tier,
            status=status,
        )
        self.db.add(candidate_skill)
        self.db.flush()
        self.db.refresh(candidate_skill)
        return candidate_skill

    def commit(self) -> None:
        self.db.commit()

    def rollback(self) -> None:
        self.db.rollback()
