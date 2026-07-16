from uuid import UUID

from pydantic import BaseModel, field_validator


class BulkUploadRequest(BaseModel):
    campaign_id: UUID
    consent_confirmed: bool

    @field_validator("consent_confirmed")
    @classmethod
    def _require_consent(cls, value: bool) -> bool:
        if not value:
            raise ValueError(
                "Consent confirmation is required before a bulk upload can be submitted."
            )
        return value
