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
    max_experience_years: float | None
    notice_period: int | None
    education_criteria: dict | None
    created_by: str
    file_path: str | None
    raw_text: str | None
    document_type: DocumentType = DocumentType.JD

    # Update/reprocess runs only — absent (existing_jd_id is None) means this
    # is a normal create run, exactly as before. When present, Persistence
    # deactivates existing_jd_id and inserts the new version row with this
    # version_number/parent_jd_id/lineage_root_id instead of hardcoded
    # 1/None/None, and the duplicate-hash check is scoped to exclude
    # lineage_root_id rather than checking every JD.
    existing_jd_id: UUID | None = None
    version_number: int = 1
    parent_jd_id: UUID | None = None
    lineage_root_id: UUID | None = None

    # Populated progressively, one stage at a time.
    source_format: JDSourceFormat | None = None
    text: str | None = None
    cleaned_text: str | None = None
    raw_extraction: dict | None = None
    extraction: JDExtractionResponse | None = None
    skill_matches: list[SkillMatchResult] | None = None
    content_hash: str | None = None
    is_duplicate: bool = False
    embedding_text: str | None = None
    embedding: list[float] | None = None
    embedding_model_version_id: UUID | None = None
    input_text_hash: str | None = None

    # Set by the Persistence stage.
    jd_id: UUID | None = None
