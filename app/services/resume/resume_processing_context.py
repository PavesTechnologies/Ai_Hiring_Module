from dataclasses import dataclass
from uuid import UUID

from app.models.async_tasks import DocumentType
from app.models.candidates import FileFormat


@dataclass
class ResumeProcessingContext:
    """
    Single, explicit contract the resume pipeline's stages read from and
    write to — mirrors JDProcessingContext's shape, kept as its own class
    rather than shared (see JDProcessingPipeline's docstring for why).
    """

    # Submitted at the start of the run — never mutated by a stage.
    task_id: str
    resume_id: UUID
    candidate_id: UUID
    file_path: str
    file_format: FileFormat
    document_type: DocumentType = DocumentType.RESUME

    # Populated progressively, one stage at a time.
    text: str | None = None
    cleaned_text: str | None = None
    raw_extraction: dict | None = None
    page_count: int | None = None
    parse_confidence_score: float | None = None
