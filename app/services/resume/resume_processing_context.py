from dataclasses import dataclass
from uuid import UUID

from app.models.async_tasks import DocumentType
from app.models.resume.resume_source_format import ResumeSourceFormat
from app.schemas.ai.resume_extraction_response import ResumeExtractionResponse
from app.services.pii.pii_types import PIIFinding
from app.services.skills.skill_normalization_service import SkillMatchResult


@dataclass
class ResumeProcessingContext:
    """
    Single, explicit contract the Resume pipeline's stages read from and
    write to, in place of ad hoc local variables threaded through
    ResumeProcessingPipeline.run() — mirrors JDProcessingContext's shape.

    Concrete to Resume by design, same reasoning as JDProcessingContext's
    own docstring: no shared base class with the JD context.
    """

    # Submitted at the start of the run — never mutated by a stage.
    task_id: str
    file_path: str
    source_format: ResumeSourceFormat
    # Optional at construction time: a caller building a context before
    # identity is known (e.g. bulk upload, ahead of Candidate/Resume
    # creation) fills these in once resolved, rather than needing them
    # up front the way an individual-upload context always has them.
    resume_id: UUID | None = None
    candidate_id: UUID | None = None
    prompt_template_id: UUID | None = None
    document_type: DocumentType = DocumentType.RESUME
    attempt_number: int = 1

    # Populated progressively, one stage at a time.
    raw_text: str | None = None
    page_count: int | None = None
    cleaned_text: str | None = None
    pii_findings: list[PIIFinding] | None = None
    redacted_text: str | None = None
    raw_extraction: dict | None = None
    validated_extraction: ResumeExtractionResponse | None = None
    skill_match_results: list[SkillMatchResult] | None = None

    # Scoring v2 (Phase 1). parse_cache_enabled: reuse a cached extraction
    # of identical input (on only when a campaign holding this resume has
    # scoring_v2_enabled). bypass_parse_cache: re-parse - always ask the
    # model, then replace the cached answer. parse_call: which
    # provider/model produced raw_extraction (the backup model if it was).
    parse_cache_enabled: bool = False
    bypass_parse_cache: bool = False
    # Re-parse of an already-parsed resume (recruiter action): the parse
    # cache is bypassed and the resume's candidate_skills are replaced, not
    # added to.
    reparse: bool = False
    parse_call: dict | None = None
