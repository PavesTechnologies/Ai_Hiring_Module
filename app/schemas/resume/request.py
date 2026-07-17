import re
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

from app.enums.constants import Jurisdiction

_EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class ResumeUploadRequest(BaseModel):
    campaign_id: UUID
    candidate_full_name: str = Field(..., min_length=1, max_length=255)
    candidate_email: str = Field(..., max_length=255)
    candidate_phone: Optional[str] = Field(default=None, max_length=50)
    jurisdiction: str = Field(default=Jurisdiction.GLOBAL.value)
    consent_confirmed: bool

    @field_validator("candidate_full_name")
    @classmethod
    def _strip_full_name(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("Candidate full name cannot be blank.")
        return stripped

    @field_validator("candidate_email")
    @classmethod
    def _validate_email_format(cls, value: str) -> str:
        stripped = value.strip()
        if not _EMAIL_PATTERN.match(stripped):
            raise ValueError("Candidate email must be a valid email address.")
        return stripped

    @field_validator("jurisdiction")
    @classmethod
    def _validate_jurisdiction(cls, value: str) -> str:
        valid = {j.value for j in Jurisdiction}
        if value not in valid:
            raise ValueError(f"jurisdiction must be one of {sorted(valid)}.")
        return value

    @field_validator("consent_confirmed")
    @classmethod
    def _require_consent(cls, value: bool) -> bool:
        if not value:
            raise ValueError(
                "Consent confirmation is required before a resume can be uploaded."
            )
        return value
