from dataclasses import dataclass
from uuid import UUID

from app.models.async_tasks import DocumentType
from app.models.jd.job_descriptions import JDSourceFormat
from app.schemas.ai.jd_extraction_response import JDExtractionResponse
from app.services.skills.skill_normalization_service import SkillMatchResult


@dataclass
class JDProcessingContext:
    """
    Single, explicit contract the JD pipeline's stages read from and write
    to, in place of ad hoc local variables threaded through JDProcessingPipeline.run().

    Concrete to JD by design: a future Resume pipeline defines its own
    analogous context following this same shape rather than sharing this
    one — see JDProcessingPipeline's own docstring for why no shared base
    class exists between the two.
    """

    # Submitted at the start of the run — never mutated by a stage.
    task_id: str
    title: str
    jurisdiction: str
    min_experience_years: float | None
    education_criteria: dict | None
    created_by: str
    file_path: str | None
    raw_text: str | None
    document_type: DocumentType = DocumentType.JD

    # Populated progressively, one stage at a time.
    source_format: JDSourceFormat | None = None
    text: str | None = None
    cleaned_text: str | None = None
    raw_extraction: dict | None = None
    extraction: JDExtractionResponse | None = None
    skill_matches: list[SkillMatchResult] | None = None
    content_hash: str | None = None
    embedding_text: str | None = None
    embedding: list[float] | None = None
    embedding_model_version_id: UUID | None = None
    input_text_hash: str | None = None

    # Set by the Persistence stage.
    jd_id: UUID | None = None
