import logging
from uuid import UUID

from app.enums.constants import ActionType, EntityType
from app.models.candidates import ParseAttemptStatus, ParseStatus, Resume
from app.repositories.resume_repository import ResumeRepository
from app.repositories.skill_repository import SkillRepository
from app.schemas.ai.resume_extraction_response import ResumeExtractionResponse
from app.services.audit_service import AuditService
from app.services.skills.skill_normalization_service import (
    SkillMatchResult,
    scoring_weight_for_tier,
    verification_status_for_tier,
)

logger = logging.getLogger(__name__)


class ResumeService:
    """
    Orchestrates the Persistence stage of the async Resume processing
    pipeline — mirrors JDService.persist_processed_jd's atomicity pattern
    (one transaction, roll back on any failure) for the Resume side.

    Distinct from ResumeUploadService (app/services/resume/resume_upload_service.py),
    which owns the synchronous upload leg (validate/store/candidate/create) —
    the two were originally both named ResumeService on separate branches
    before this merge.
    """

    # Real bucket name, confirmed live against Supabase (see
    # docs/resume_intake_implementation_log.md's bucket-name-mismatch entry) —
    # not "airs-resumes".
    RESUME_STORAGE_BUCKET = "airs_resumes"
    PARSER_VERSION = "gemini-resume-extraction-v1"
    PARSER_NAME = "gemini-resume-extraction"

    def __init__(
        self,
        repository: ResumeRepository,
        audit_service: AuditService,
    ):
        self.repository = repository
        self.audit_service = audit_service

    def persist_processed_resume(
        self,
        *,
        resume: Resume,
        extraction: ResumeExtractionResponse,
        skill_repository: SkillRepository,
        skill_matches: list[SkillMatchResult],
        attempt_number: int | None = None,
        page_count: int | None = None,
    ) -> UUID:
        """
        Writes Resume.parsed_json/parse_status + candidate_skills + audit
        log in one transaction. Returns the resume_id (the row already
        exists — this pipeline never creates one, per the scope boundary
        that Candidate/Resume creation happens elsewhere).

        M08-E01: no longer writes resume_embeddings here - embedding
        generation moved to its own decoupled EMBED_RESUME Celery task
        (app/tasks/embedding_tasks.py), enqueued after this method's caller
        (ResumeProcessingPipeline) succeeds, so a resume never gets two
        embedding rows.

        attempt_number is optional: when a caller supplies it, a
        resume_parse_attempts row is recorded here too. Bulk upload already
        records its own attempt separately (at resume-creation time, before
        this method ever runs) and does not pass this — so this stays
        opt-in rather than unconditional, to avoid a second, duplicate
        attempt row for bulk-origin resumes.
        """
        logger.warning("=== persist_processed_resume STARTED === resume_id=%s", resume.id)
        try:
            self.repository.update_parsed_result(
                resume,
                parsed_json=extraction.model_dump(mode="json"),
                parse_status=ParseStatus.PARSED,
                parser_version=self.PARSER_VERSION,
                page_count=page_count,
            )
            logger.warning(
                "=== persist_processed_resume: parsed_json/parse_status updated === resume_id=%s", resume.id,
            )

            # Two raw strings (e.g. "Python" in one bullet, "PYTHON" in
            # another) can resolve to the same canonical_skill_id via the
            # case/rule/fuzzy tiers — candidate_skills has a
            # (resume_id, canonical_skill_id) unique constraint (for
            # non-null canonical_skill_id), so a second insert for the same
            # skill would fail the whole transaction. Collapse to one match
            # per canonical skill before persisting, same as JDService does
            # for jd_skills.
            matched_by_skill: dict[UUID, SkillMatchResult] = {}
            for match in skill_matches:
                if not match.canonical_skill_id:
                    continue
                if match.canonical_skill_id not in matched_by_skill:
                    matched_by_skill[match.canonical_skill_id] = match

            for match in matched_by_skill.values():
                self.repository.create_candidate_skill(
                    candidate_id=resume.candidate_id,
                    resume_id=resume.id,
                    canonical_skill_id=match.canonical_skill_id,
                    raw_extracted_text=match.raw_text,
                    confidence=match.confidence,
                    match_tier=match.match_tier.value,
                    status=verification_status_for_tier(match.match_tier).value,
                    scoring_weight=scoring_weight_for_tier(match.match_tier),
                )
                skill_repository.bump_occurrence_count(match.canonical_skill_id)
            logger.warning(
                "=== persist_processed_resume: matched candidate_skills persisted === "
                "resume_id=%s count=%s", resume.id, len(matched_by_skill),
            )

            # Unmatched skills have no canonical_skill_id, so the unique
            # constraint (scoped to non-null canonical_skill_id) doesn't
            # apply — every unknown raw skill gets its own candidate_skills
            # row directly (unlike JD, which only stores matches in
            # jd_skills). Each one also dedupes into the same unknown_skills
            # pool JD unmatched skills already share, via the same
            # upsert_unknown_skill used by JDService — a resume's "X" and a
            # JD's "X" merge into one UnknownSkill row/frequency counter.
            newly_created_unknown_skills = []
            for match in skill_matches:
                if match.canonical_skill_id:
                    continue
                unknown_skill, was_created = skill_repository.upsert_unknown_skill(
                    match.raw_text, normalized_key=match.normalized_text
                )
                self.repository.create_candidate_skill(
                    candidate_id=resume.candidate_id,
                    resume_id=resume.id,
                    canonical_skill_id=None,
                    raw_extracted_text=match.raw_text,
                    confidence=match.confidence,
                    match_tier=match.match_tier.value,
                    status=verification_status_for_tier(match.match_tier).value,
                    scoring_weight=scoring_weight_for_tier(match.match_tier),
                    unknown_skill_id=unknown_skill.id,
                )
                if was_created:
                    newly_created_unknown_skills.append(unknown_skill)
            logger.warning(
                "=== persist_processed_resume: unmatched candidate_skills persisted === resume_id=%s", resume.id,
            )

            if attempt_number is not None:
                self.repository.record_parse_attempt(
                    resume_id=resume.id,
                    attempt_number=attempt_number,
                    parser_used=self.PARSER_NAME,
                    parser_version=self.PARSER_VERSION,
                    status=ParseAttemptStatus.SUCCESS,
                    confidence_score=1.0,
                )

            self.audit_service.log(
                actor_id=resume.uploaded_by,
                actor_role="HR_ADMIN",
                action_type=ActionType.RESUME_PARSED,
                entity_type=EntityType.RESUME,
                entity_id=resume.id,
                jurisdiction=None,
                details={
                    "resume_id": str(resume.id),
                    "candidate_id": str(resume.candidate_id),
                },
            )

            for match in matched_by_skill.values():
                self.audit_service.log(
                    actor_id=resume.uploaded_by,
                    actor_role="HR_ADMIN",
                    action_type=ActionType.CANDIDATE_SKILL_MATCHED,
                    entity_type=EntityType.CANDIDATE_SKILL,
                    entity_id=resume.id,
                    jurisdiction=None,
                    details={
                        "resume_id": str(resume.id),
                        "canonical_skill_id": str(match.canonical_skill_id),
                        "match_tier": match.match_tier.value,
                    },
                )

            for unknown_skill in newly_created_unknown_skills:
                self.audit_service.log(
                    actor_id=resume.uploaded_by,
                    actor_role="HR_ADMIN",
                    action_type=ActionType.UNKNOWN_SKILL_CREATED,
                    entity_type=EntityType.UNKNOWN_SKILL,
                    entity_id=unknown_skill.id,
                    jurisdiction=None,
                    details={
                        "raw_text": unknown_skill.raw_text,
                        "resume_id": str(resume.id),
                        "candidate_id": str(resume.candidate_id),
                    },
                )
            logger.warning(
                "=== persist_processed_resume: audit logs written === resume_id=%s", resume.id,
            )

            self.repository.commit()
            logger.warning("=== persist_processed_resume COMMITTED === resume_id=%s", resume.id)
            return resume.id

        except Exception:
            logger.warning("=== persist_processed_resume FAILED - rolling back === resume_id=%s", resume.id)
            self.repository.rollback()
            raise
