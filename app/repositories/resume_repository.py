from datetime import datetime
from uuid import UUID

from sqlalchemy import delete, func, or_, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.candidates import Candidate, ParseAttemptStatus, ParseStatus, Resume, ResumeParseAttempt
from app.models.embeddings import EmbeddingModelVersion, ResumeEmbedding
from app.models.jd.job_descriptions import JDEmbedding
from app.models.pipeline import CampaignCandidate, PipelineStage
from app.models.skills import CandidateSkill, SkillOntology
from app.repositories.embedding_model_version_repository import EmbeddingModelVersionRepository

_SORT_COLUMNS = {
    "created_at": Resume.created_at,
    "parse_status": Resume.parse_status,
}

# Embedding Storage Dashboard - the ivfflat index created by
# alembic/versions/b3e7a1c9d5f2_resume_embeddings_ivfflat_index.py.
RESUME_EMBEDDINGS_IVFFLAT_INDEX = "idx_resume_embeddings_embedding"


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

    def get_active_by_candidate_ids(self, candidate_ids: list[UUID]) -> dict[UUID, Resume]:
        """
        Global Candidates directory (GET /candidates) - batched counterpart
        to get_active_by_candidate, one query for a whole page of
        candidates rather than one per row. Keyed by candidate_id; a
        candidate with no active resume version is simply absent from the
        returned dict.
        """
        if not candidate_ids:
            return {}
        stmt = select(Resume).where(
            Resume.candidate_id.in_(candidate_ids),
            Resume.is_active_version.is_(True),
        )
        return {resume.candidate_id: resume for resume in self.db.execute(stmt).scalars().all()}

    def get_by_file_hash_global(self, file_hash: str) -> Resume | None:
        """
        Epic 3 (M05-E03) Phase C2 — exact-duplicate check. Deliberately
        unscoped by candidate: a byte-identical file can be re-uploaded
        under a different claimed name/email, and the whole point of this
        check is to catch that regardless of what identity was typed into
        this request's form fields.
        """
        stmt = select(Resume).where(Resume.file_hash == file_hash)
        return self.db.execute(stmt).scalars().first()

    def get_max_version_number(self, candidate_id: UUID) -> int:
        """
        Epic 3 (M05-E03) Phase C1. Only ever called on the resubmission path
        (an active resume for this candidate was already found), so this
        always sees at least one row in practice; the `or 0` fallback is
        defensive, not a real code path.
        """
        stmt = select(func.max(Resume.version_number)).where(Resume.candidate_id == candidate_id)
        return self.db.execute(stmt).scalar() or 0

    def deactivate_active_version(self, candidate_id: UUID) -> None:
        """
        Epic 3 (M05-E03) Phase C1. A single atomic UPDATE, not a
        read-modify-write — two concurrent resubmissions for the same
        candidate must not both read "no active version" and both insert
        as the same next version number. This closes that race for the
        deactivation step itself; it does not fully eliminate every
        concurrent-version-number race on its own (e.g. two callers can
        still both call get_max_version_number before either has inserted
        its new row) — resumes has no DB-level unique constraint on
        (candidate_id, version_number) to catch that at the database
        layer. Tightening that further is a future enhancement, not part
        of this phase's scope.
        """
        self.db.execute(
            update(Resume)
            .where(Resume.candidate_id == candidate_id, Resume.is_active_version.is_(True))
            .values(is_active_version=False)
        )
        self.db.flush()

    def get_all_versions_by_candidate(self, candidate_id: UUID) -> list[Resume]:
        """Epic 3 (M05-E03) Phase C1 — full version history, most recent first. Monitoring-only, no writes."""
        stmt = (
            select(Resume)
            .where(Resume.candidate_id == candidate_id)
            .order_by(Resume.version_number.desc())
        )
        return list(self.db.execute(stmt).scalars().all())

    def get_by_skill_match(self, canonical_skill_id: UUID | None, raw_text_pattern: str) -> list[Resume]:
        """
        M13-E01 S02 (Talent Pool Search) - every resume with a
        candidate_skills row matching either the resolved canonical skill
        or the raw extracted text pattern (caller passes an already
        LIKE-escaped '%...%' pattern). Unfiltered by eligibility -
        TalentPoolService applies ResumeSelectionService's own eligibility
        predicate afterward, so this stays a plain data lookup, never a
        second eligibility implementation. Most recent first, so a
        candidate matched on more than one resume version consistently
        surfaces their newest one.
        """
        conditions = [CandidateSkill.raw_extracted_text.ilike(raw_text_pattern, escape="\\")]
        if canonical_skill_id is not None:
            conditions.append(CandidateSkill.canonical_skill_id == canonical_skill_id)
        stmt = (
            select(Resume)
            .join(CandidateSkill, CandidateSkill.resume_id == Resume.id)
            .where(or_(*conditions))
            .order_by(Resume.created_at.desc())
            .distinct()
        )
        return list(self.db.execute(stmt).scalars().all())

    def get_all_parsed(self) -> list[Resume]:
        """M13-E01 S02 (Talent Pool Search) - every PARSED resume, the base set when the search has no skill filter."""
        stmt = (
            select(Resume)
            .where(Resume.parse_status == ParseStatus.PARSED)
            .order_by(Resume.created_at.desc())
        )
        return list(self.db.execute(stmt).scalars().all())

    def delete(self, resume: Resume) -> None:
        """Candidate erasure — hard-deletes a single resume version's row (caller has already removed its file from storage and its child rows)."""
        self.db.delete(resume)
        self.db.flush()

    def delete_parse_attempts(self, resume_id: UUID) -> None:
        """Candidate erasure — removes resume_parse_attempts for one resume version."""
        self.db.execute(delete(ResumeParseAttempt).where(ResumeParseAttempt.resume_id == resume_id))
        self.db.flush()

    def delete_embeddings_by_candidate(self, candidate_id: UUID) -> None:
        """Candidate erasure — resume_embeddings has no FK constraint at all, so this is the only way to avoid orphaning it."""
        self.db.execute(delete(ResumeEmbedding).where(ResumeEmbedding.candidate_id == candidate_id))
        self.db.flush()

    def delete_candidate_skills_by_candidate(self, candidate_id: UUID) -> None:
        """Candidate erasure — read counterpart is get_candidate_skills; this removes every candidate_skills row for the candidate."""
        self.db.execute(delete(CandidateSkill).where(CandidateSkill.candidate_id == candidate_id))
        self.db.flush()

    def delete_embedding_by_resume(self, resume_id: UUID) -> None:
        """
        Single-resume cleanup — narrower than delete_embeddings_by_candidate,
        which would also remove embeddings for this candidate's OTHER resume
        versions. resume_embeddings has no FK constraint at all, so this is
        the only way to avoid orphaning it.
        """
        self.db.execute(delete(ResumeEmbedding).where(ResumeEmbedding.resume_id == resume_id))
        self.db.flush()

    def delete_candidate_skills_by_resume(self, resume_id: UUID) -> None:
        """Single-resume cleanup — narrower than delete_candidate_skills_by_candidate, scoped to just this resume version's skills."""
        self.db.execute(delete(CandidateSkill).where(CandidateSkill.resume_id == resume_id))
        self.db.flush()

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
        page_count: int | None = None,
    ) -> Resume:
        resume.parsed_json = parsed_json
        resume.parse_status = parse_status
        resume.parser_version = parser_version
        # None means "this attempt didn't compute a page count" (e.g. bulk
        # upload sets it directly at Resume-creation time, before this pipeline
        # ever runs) — never overwrite an already-known value with null.
        if page_count is not None:
            resume.page_count = page_count
        self.db.flush()
        self.db.refresh(resume)
        return resume

    def mark_parse_failed(self, resume: Resume) -> Resume:
        resume.parse_status = ParseStatus.FAILED
        self.db.flush()
        self.db.refresh(resume)
        return resume

    def mark_parse_pending(self, resume: Resume) -> Resume:
        """Epic 4 (M05-E04) Phase D10 - flips a FAILED resume back to PENDING before re-dispatching it (retry/DLQ replay)."""
        resume.parse_status = ParseStatus.PENDING
        self.db.flush()
        self.db.refresh(resume)
        return resume

    def set_task_id(self, resume: Resume, task_id: str) -> Resume:
        resume.task_id = task_id
        self.db.flush()
        self.db.refresh(resume)
        return resume

    def get_active_embedding_model_version(self) -> EmbeddingModelVersion:
        return EmbeddingModelVersionRepository(self.db).get_active()

    def create_resume_embedding(
        self,
        resume_id: UUID,
        candidate_id: UUID,
        embedding: list[float],
        embedding_model_version_id: UUID,
        input_text_hash: str,
        is_anonymized: bool = True,
        is_talent_pool_eligible: bool = True,
    ) -> tuple[ResumeEmbedding, bool]:
        """
        Returns (resume_embedding, was_created). uq_resume_embeddings_resume_model_version
        (resume_id, embedding_model_version_id) backs this at the DB level —
        two concurrent EMBED_RESUME runs for the same resume (broker
        redelivery of a crashed RUNNING task, a manual re-trigger, etc.) can
        both pass the application-level dedup check before either commits.
        A SAVEPOINT scopes the loser's IntegrityError to just this insert
        attempt (mirrors CandidateRepository.create's pattern for the same
        class of race), then falls back to the row the winner already
        committed instead of raising.
        """
        resume_embedding = ResumeEmbedding(
            resume_id=resume_id,
            candidate_id=candidate_id,
            embedding=embedding,
            embedding_model_version_id=embedding_model_version_id,
            input_text_hash=input_text_hash,
            is_anonymized=is_anonymized,
            is_talent_pool_eligible=is_talent_pool_eligible,
        )
        try:
            with self.db.begin_nested():
                self.db.add(resume_embedding)
                self.db.flush()
            self.db.refresh(resume_embedding)
            return resume_embedding, True
        except IntegrityError:
            existing = (
                self.db.query(ResumeEmbedding)
                .filter(
                    ResumeEmbedding.resume_id == resume_id,
                    ResumeEmbedding.embedding_model_version_id == embedding_model_version_id,
                )
                .first()
            )
            return existing, False

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
        unknown_skill_id: UUID | None = None,
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
            unknown_skill_id=unknown_skill_id,
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

    def get_top_skills_by_candidate(self, candidate_id: UUID, limit: int = 5) -> list[str]:
        """
        Talent Pool (M13-E01 S01 T02) — the candidate's most frequently
        matched canonical skills across every resume version/campaign
        submission, most-occurrences-first. Only canonical (matched) skills
        are counted — CandidateSkill rows with no canonical_skill_id
        (UNKNOWN tier) have no stable display name to rank them by, so
        they're excluded rather than shown as raw_extracted_text.
        """
        stmt = (
            select(SkillOntology.canonical_name, func.count(CandidateSkill.id))
            .join(CandidateSkill, CandidateSkill.canonical_skill_id == SkillOntology.id)
            .where(CandidateSkill.candidate_id == candidate_id)
            .group_by(SkillOntology.canonical_name)
            .order_by(func.count(CandidateSkill.id).desc())
            .limit(limit)
        )
        return [name for name, _ in self.db.execute(stmt).all()]

    def get_canonical_skills_by_resume_ids(self, resume_ids: list[UUID]) -> dict[UUID, list[str]]:
        """
        Talent Pool Search (M13-E01 S02 T0x) - every distinct canonical/
        normalized skill name for a batch of resumes in ONE query, keyed by
        resume_id. Batched counterpart to get_top_skills_by_candidate: that
        method is per-candidate, top-N, and frequency-ranked (for the
        candidate profile page); this is per-resume, unranked, and returns
        every match (for the Talent Pool list page, so a page of candidates
        never issues one skills query per row). Only canonical (matched)
        skills are included - CandidateSkill rows with no canonical_skill_id
        (UNKNOWN tier) have no stable display name, same exclusion
        get_top_skills_by_candidate already applies.
        """
        if not resume_ids:
            return {}
        stmt = (
            select(CandidateSkill.resume_id, SkillOntology.canonical_name)
            .join(SkillOntology, CandidateSkill.canonical_skill_id == SkillOntology.id)
            .where(CandidateSkill.resume_id.in_(resume_ids))
            .distinct()
        )
        skills_by_resume_id: dict[UUID, list[str]] = {}
        for resume_id, canonical_name in self.db.execute(stmt).all():
            skills_by_resume_id.setdefault(resume_id, []).append(canonical_name)
        return skills_by_resume_id

    def get_embedding(self, resume_id: UUID) -> ResumeEmbedding | None:
        """Read counterpart to create_resume_embedding — monitoring-only, no writes."""
        stmt = select(ResumeEmbedding).where(ResumeEmbedding.resume_id == resume_id)
        return self.db.execute(stmt).scalars().first()

    def get_embeddings_by_candidate(self, candidate_id: UUID) -> list[ResumeEmbedding]:
        """
        Daily talent-pool-eligibility reconciliation - every
        resume_embeddings row for one candidate (a candidate can have more
        than one resume version), read-only, so the reconciliation task
        can compare each row's current is_talent_pool_eligible against
        what it should be before deciding whether an UPDATE is even needed.
        """
        stmt = select(ResumeEmbedding).where(ResumeEmbedding.candidate_id == candidate_id)
        return list(self.db.execute(stmt).scalars().all())

    def get_cosine_similarity(self, resume_embedding_id: UUID, target_vector: list[float]) -> float | None:
        """
        M08-E02: cosine similarity between one resume_embeddings row and any
        other embedding vector (a JD's, in Semantic Matching's case),
        computed by pgvector itself (Vector.cosine_distance, the same
        comparator SkillRepository's semantic-match queries already use) -
        never a manual Python dot-product/norm calculation. Returns
        1 - cosine_distance, or None if resume_embedding_id doesn't exist.
        """
        distance = ResumeEmbedding.embedding.cosine_distance(target_vector)
        stmt = select(distance).where(ResumeEmbedding.id == resume_embedding_id)
        result = self.db.execute(stmt).scalar_one_or_none()
        return None if result is None else 1.0 - result

    def compute_semantic_similarity(self, resume_id: UUID, jd_id: UUID) -> float | None:
        """
        Story 538: the entire formula - 1 - (re.embedding <=> je.embedding) -
        runs as one statement in Postgres via pgvector's <=> operator,
        filtered directly by resume_id/jd_id; the raw vectors never travel
        into application memory (unlike get_cosine_similarity, which
        requires the caller to have already fetched one embedding's full
        vector). Not an ivfflat nearest-neighbour scan - a pairwise
        comparison of two already-known vectors has no use for an ANN
        index, which only helps "find the closest among many."
        embedding_model_version_id is required to match as part of the same
        query - a mismatch (or either row not existing) returns None rather
        than a meaningless cross-version distance. resume_embeddings'
        (resume_id, embedding_model_version_id) uniqueness constraint means
        this equality-filtered join can return at most one row even when a
        resume has embeddings under more than one model version.
        """
        similarity = ResumeEmbedding.embedding.cosine_distance(JDEmbedding.embedding)
        stmt = (
            select(1 - similarity)
            .select_from(ResumeEmbedding, JDEmbedding)
            .where(
                ResumeEmbedding.resume_id == resume_id,
                JDEmbedding.jd_id == jd_id,
                ResumeEmbedding.embedding_model_version_id == JDEmbedding.embedding_model_version_id,
            )
        )
        result = self.db.execute(stmt).scalar_one_or_none()
        return None if result is None else float(result)

    def count_embeddings(self) -> int:
        """Embedding Storage Dashboard - total resume_embeddings row count."""
        return self.db.query(func.count(ResumeEmbedding.id)).scalar() or 0

    def get_ivfflat_index_health(self) -> dict:
        """
        Embedding Storage Dashboard - reads Postgres's own
        pg_stat_user_indexes for RESUME_EMBEDDINGS_IVFFLAT_INDEX (no
        ORM-level equivalent exists for index introspection, so this is
        deliberately raw SQL, same convention as other admin/introspection
        queries in this codebase). "exists": False means the index is
        missing entirely (e.g. a downgrade ran, or a fresh DB the
        migration never reached) - a real production concern distinct
        from "exists but a REINDEX would help."
        """
        row = self.db.execute(
            text(
                """
                SELECT
                    s.indexrelname AS index_name,
                    pg_relation_size(s.indexrelid) AS size_bytes,
                    s.idx_scan AS scan_count
                FROM pg_stat_user_indexes s
                WHERE s.relname = 'resume_embeddings' AND s.indexrelname = :index_name
                """
            ),
            {"index_name": RESUME_EMBEDDINGS_IVFFLAT_INDEX},
        ).first()

        if row is None:
            return {"exists": False, "index_name": RESUME_EMBEDDINGS_IVFFLAT_INDEX, "size_bytes": None, "scan_count": None}
        return {
            "exists": True,
            "index_name": row.index_name,
            "size_bytes": row.size_bytes,
            "scan_count": row.scan_count,
        }

    def get_distinct_candidate_ids_with_embeddings(self) -> list[UUID]:
        """
        Daily talent-pool-eligibility reconciliation - every candidate_id
        that has at least one resume_embeddings row, so the reconciliation
        task only ever evaluates candidates that actually have an embedding
        to correct (never every candidate in the system).
        """
        stmt = select(ResumeEmbedding.candidate_id).distinct()
        return list(self.db.execute(stmt).scalars().all())

    def set_talent_pool_eligibility_for_candidate(self, candidate_id: UUID, eligible: bool) -> int:
        """
        Bulk-updates is_talent_pool_eligible on every resume_embeddings row
        for one candidate (a candidate can have more than one resume
        version, each with its own embedding row) - used by both the daily
        reconciliation task (either direction) and anywhere else that only
        needs to flip eligibility without touching the vector itself
        (unlike zero_out_embeddings_for_candidate, which is erasure-specific
        and also destroys the vector). Returns the number of rows updated.
        """
        result = self.db.execute(
            update(ResumeEmbedding)
            .where(ResumeEmbedding.candidate_id == candidate_id)
            .values(is_talent_pool_eligible=eligible)
        )
        self.db.flush()
        return result.rowcount

    def zero_out_embeddings_for_candidate(self, candidate_id: UUID) -> int:
        """
        Candidate erasure (requested phase - see CandidateErasureService.
        request_erasure): overwrites the embedding vector itself with a
        384-dimension zero vector and marks every row
        is_talent_pool_eligible=False, for every resume_embeddings row
        belonging to this candidate - the rows are NEVER deleted (retained
        for referential integrity, per the erasure-request requirement,
        unlike the full erase_candidate() hard-delete flow's
        delete_embeddings_by_candidate). jd_embeddings is never touched
        here - JDs are never candidate PII. Returns the number of rows
        updated.
        """
        zero_vector = [0.0] * 384
        result = self.db.execute(
            update(ResumeEmbedding)
            .where(ResumeEmbedding.candidate_id == candidate_id)
            .values(embedding=zero_vector, is_talent_pool_eligible=False)
        )
        self.db.flush()
        return result.rowcount

    def get_embedding_by_hash(
        self, input_text_hash: str, embedding_model_version_id: UUID,
    ) -> ResumeEmbedding | None:
        """
        M08-E01 T05: dedup lookup for EMBED_RESUME - any existing
        resume_embeddings row (for ANY resume) whose input_text_hash and
        embedding_model_version_id both match. When found, the caller
        copies its embedding vector onto a new row for the current
        resume_id instead of calling the embedding service again.
        """
        stmt = select(ResumeEmbedding).where(
            ResumeEmbedding.input_text_hash == input_text_hash,
            ResumeEmbedding.embedding_model_version_id == embedding_model_version_id,
        )
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
        uploaded_by: str | None = None,
        page: int = 1,
        size: int = 20,
        sort_by: str = "created_at",
        sort_dir: str = "desc",
    ) -> list[Resume]:
        """Monitoring-only, no writes. Backs GET /resumes' list/search/filter."""
        conditions = self._build_search_conditions(
            campaign_id, parse_status, source, email_hash, uploaded_from, uploaded_to, uploaded_by,
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
        uploaded_by: str | None = None,
    ) -> int:
        """Same filters as search(), for the list endpoint's total count."""
        conditions = self._build_search_conditions(
            campaign_id, parse_status, source, email_hash, uploaded_from, uploaded_to, uploaded_by,
        )
        stmt = select(func.count()).select_from(Resume).where(*conditions)
        return self.db.execute(stmt).scalar_one()

    def get_campaign_history_entries(
        self,
        campaign_id: UUID,
    ) -> list[tuple[Resume, PipelineStage | None]]:
        """
        Epic 4 (M05-E04) Phase D7 — every individual (non-bulk) resume
        actually linked to this campaign, most recent first, paired with
        its campaign_candidates.pipeline_stage in the SAME query — avoids
        an N+1 per-row lookup. Deliberately a dedicated method rather than
        widening search()/count_search()'s shared return shape (those two
        are also used by the general, not-campaign-scoped /resumes list,
        where "the" pipeline_stage wouldn't even be well-defined).

        Deliberately unfiltered beyond campaign scope — UploadHistoryService
        applies uploaded_by/date/outcome filters in Python over this full
        set, so the uploader-dropdown it derives always lists every real
        uploader for the campaign, never shrinking as other filters are
        applied. Bounded by per-campaign upload volume, same tradeoff D5
        already accepted for its own per-job file log.

        A real (inner) join, not a LEFT JOIN: a resume only "belongs to"
        this campaign at all via a matching campaign_candidates row (e.g.
        the orphan-Resume race documented elsewhere in this codebase,
        where create_campaign_candidate failed after the Resume row was
        already committed, leaves a resume belonging to NO campaign) — it
        should not appear in this campaign's history if that link never
        existed. The join is scoped to this exact campaign_id (not resume_id
        alone), so it only fans out a row if this resume were somehow
        linked twice to the SAME campaign — not expected in practice (a
        resume's candidate_id is fixed, and every code path that creates
        campaign_candidates rows keys off that same candidate_id), the same
        assumption get_by_resume_id's own docstring already relies on.
        """
        stmt = (
            select(Resume, CampaignCandidate.pipeline_stage)
            .join(
                CampaignCandidate,
                (CampaignCandidate.resume_id == Resume.id)
                & (CampaignCandidate.campaign_id == campaign_id),
            )
            .where(Resume.bulk_upload_job_id.is_(None))
            .order_by(Resume.created_at.desc())
        )
        return list(self.db.execute(stmt).all())

    @staticmethod
    def _build_search_conditions(
        campaign_id: UUID | None,
        parse_status: ParseStatus | None,
        source: str | None,
        email_hash: str | None,
        uploaded_from: datetime | None,
        uploaded_to: datetime | None,
        uploaded_by: str | None = None,
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
