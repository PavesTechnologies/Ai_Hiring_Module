from uuid import UUID

from pydantic import BaseModel, Field, field_validator

from app.enums.constants import Jurisdiction


class BulkUploadRequest(BaseModel):
    campaign_id: UUID
    jurisdiction: str = Field(default=Jurisdiction.GLOBAL.value)
    consent_confirmed: bool

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
                "Consent confirmation is required before a bulk upload can be submitted."
            )
        return value
