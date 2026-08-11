from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field

from app.models.candidates import ParseStatus


class CandidateResumeSummary(BaseModel):
    """Latest/current (is_active_version) resume - informational only, never a campaign-specific selection."""
    resume_id: UUID
    version_number: int
    parse_status: ParseStatus | None = None
    uploaded_at: datetime | None = None


class CandidateDirectoryItem(BaseModel):
    candidate_id: UUID
    full_name: str | None = None
    # Masked, never the raw decrypted address - same convention as
    # TalentPoolService._mask_email.
    email: str | None = None
    designation: str | None = None
    location: str | None = None
    experience: float | None = None
    jurisdiction: str
    # None when the candidate has no resume yet (uploaded but not parsed,
    # or a candidate row created without one).
    resume: CandidateResumeSummary | None = None
    skills: list[str] = Field(default_factory=list)
    created_at: datetime


class CandidateDirectoryResponse(BaseModel):
    items: list[CandidateDirectoryItem]
    total: int
    page: int
    size: int
