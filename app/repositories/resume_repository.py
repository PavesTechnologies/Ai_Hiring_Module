from datetime import datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.candidates import Candidate, ParseAttemptStatus, ParseStatus, Resume, ResumeParseAttempt
from app.models.embeddings import EmbeddingModelVersion, ResumeEmbedding
from app.models.pipeline import CampaignCandidate
from app.models.skills import CandidateSkill

_SORT_COLUMNS = {
    "created_at": Resume.created_at,
    "parse_status": Resume.parse_status,
}


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

    def set_task_id(self, resume: Resume, task_id: str) -> Resume:
        resume.task_id = task_id
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

    def get_parse_attempts(self, resume_id: UUID) -> list[ResumeParseAttempt]:
        """Read counterpart to record_parse_attempt — monitoring-only, no writes."""
        stmt = (
            select(ResumeParseAttempt)
            .where(ResumeParseAttempt.resume_id == resume_id)
            .order_by(ResumeParseAttempt.attempted_at)
        )
        return list(self.db.execute(stmt).scalars().all())

    def get_candidate_skills(self, resume_id: UUID) -> list[CandidateSkill]:
        """Read counterpart to create_candidate_skill — monitoring-only, no writes."""
        stmt = select(CandidateSkill).where(CandidateSkill.resume_id == resume_id)
        return list(self.db.execute(stmt).scalars().all())

    def get_embedding(self, resume_id: UUID) -> ResumeEmbedding | None:
        """Read counterpart to create_resume_embedding — monitoring-only, no writes."""
        stmt = select(ResumeEmbedding).where(ResumeEmbedding.resume_id == resume_id)
        return self.db.execute(stmt).scalars().first()

    def get_by_file_path(self, file_path: str) -> Resume | None:
        """
        Monitoring-only. bulk_upload_job_files carries no resume_id column —
        a bulk file's resulting Resume row (if any) is found via the storage
        path both share: parse_bulk_upload_file sets Resume.file_path to the
        exact same value as the file's own storage_path, and that path
        embeds a fresh uuid4() per file, so the match is reliably 1:1.
        """
        stmt = select(Resume).where(Resume.file_path == file_path)
        return self.db.execute(stmt).scalars().first()

    def search(
        self,
        *,
        campaign_id: UUID | None = None,
        parse_status: ParseStatus | None = None,
        source: str | None = None,
        email_hash: str | None = None,
        uploaded_from: datetime | None = None,
        uploaded_to: datetime | None = None,
        page: int = 1,
        size: int = 20,
        sort_by: str = "created_at",
        sort_dir: str = "desc",
    ) -> list[Resume]:
        """Monitoring-only, no writes. Backs GET /resumes' list/search/filter."""
        conditions = self._build_search_conditions(
            campaign_id, parse_status, source, email_hash, uploaded_from, uploaded_to,
        )
        sort_column = _SORT_COLUMNS.get(sort_by, Resume.created_at)
        order = sort_column.asc() if sort_dir == "asc" else sort_column.desc()

        stmt = (
            select(Resume)
            .where(*conditions)
            .order_by(order)
            .offset((page - 1) * size)
            .limit(size)
        )
        return list(self.db.execute(stmt).scalars().all())

    def count_search(
        self,
        *,
        campaign_id: UUID | None = None,
        parse_status: ParseStatus | None = None,
        source: str | None = None,
        email_hash: str | None = None,
        uploaded_from: datetime | None = None,
        uploaded_to: datetime | None = None,
    ) -> int:
        """Same filters as search(), for the list endpoint's total count."""
        conditions = self._build_search_conditions(
            campaign_id, parse_status, source, email_hash, uploaded_from, uploaded_to,
        )
        stmt = select(func.count()).select_from(Resume).where(*conditions)
        return self.db.execute(stmt).scalar_one()

    @staticmethod
    def _build_search_conditions(
        campaign_id: UUID | None,
        parse_status: ParseStatus | None,
        source: str | None,
        email_hash: str | None,
        uploaded_from: datetime | None,
        uploaded_to: datetime | None,
    ) -> list:
        # Resume carries no campaign_id column itself — reached only via
        # campaign_candidates. A subquery (not a join) avoids duplicating a
        # resume row if it were ever linked to more than one
        # campaign_candidates record for the same campaign.
        conditions = []
        if campaign_id is not None:
            resume_ids_in_campaign = select(CampaignCandidate.resume_id).where(
                CampaignCandidate.campaign_id == campaign_id
            )
            conditions.append(Resume.id.in_(resume_ids_in_campaign))
        if parse_status is not None:
            conditions.append(Resume.parse_status == parse_status)
        if source == "individual":
            conditions.append(Resume.bulk_upload_job_id.is_(None))
        elif source == "bulk":
            conditions.append(Resume.bulk_upload_job_id.is_not(None))
        if email_hash is not None:
            # candidates.full_name_encrypted is encrypted at rest and can't
            # be searched directly — email_hash is the one exact-match
            # identity lookup that's actually available (see
            # docs/Resume_Intake_Monitoring_API_Design.md §8).
            candidate_ids_matching = select(Candidate.id).where(Candidate.email_hash == email_hash)
            conditions.append(Resume.candidate_id.in_(candidate_ids_matching))
        if uploaded_from is not None:
            conditions.append(Resume.created_at >= uploaded_from)
        if uploaded_to is not None:
            conditions.append(Resume.created_at <= uploaded_to)
        return conditions

    def commit(self) -> None:
        self.db.commit()

    def rollback(self) -> None:
        self.db.rollback()
