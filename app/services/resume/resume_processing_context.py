from dataclasses import dataclass
from uuid import UUID

from app.models.async_tasks import DocumentType
from app.models.resume.resume_source_format import ResumeSourceFormat
from app.schemas.ai.resume_extraction_response import ResumeExtractionResponse
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
    document_type: DocumentType = DocumentType.RESUME
    attempt_number: int = 1

    # Populated progressively, one stage at a time.
    raw_text: str | None = None
    cleaned_text: str | None = None
    raw_extraction: dict | None = None
    validated_extraction: ResumeExtractionResponse | None = None
    skill_match_results: list[SkillMatchResult] | None = None
    embedding_text: str | None = None
    embedding: list[float] | None = None
    embedding_model_version_id: UUID | None = None
    input_text_hash: str | None = None
