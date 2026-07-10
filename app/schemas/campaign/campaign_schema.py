from datetime import datetime
from decimal import Decimal
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, Field, field_validator


class CampaignCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)

    jd_id: UUID

    max_candidates: Optional[int] = Field(default=None, gt=0, le=100000)

    deadline: Optional[datetime] = None

    weight_deterministic: Decimal = Decimal("30.00")
    weight_semantic: Decimal = Decimal("40.00")
    weight_ai: Decimal = Decimal("30.00")

    semantic_threshold: Decimal = Decimal("0.6500")
    ai_threshold: Decimal = Decimal("50.00")

    hiring_manager_id: str
    recruiter_id: str

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str):
        value = value.strip()

        if not value:
            raise ValueError("Campaign name cannot be empty.")

        return value


class CampaignUpdateRequest(BaseModel):
    """
    PATCH body for editing a campaign — every field optional; only what is
    sent gets changed. clear_* flags exist because in a PATCH, omitting a
    field and sending null are indistinguishable after parsing, but the spec
    requires deadline/cap to be explicitly removable.
    """
    name: Optional[str] = Field(default=None, min_length=1, max_length=255)

    deadline: Optional[datetime] = None
    clear_deadline: bool = False

    max_candidates: Optional[int] = Field(default=None, gt=0, le=100000)
    clear_max_candidates: bool = False

    weight_deterministic: Optional[Decimal] = None
    weight_semantic: Optional[Decimal] = None
    weight_ai: Optional[Decimal] = None
    semantic_threshold: Optional[Decimal] = None
    ai_threshold: Optional[Decimal] = None

    # The "I understand existing scores won't be recalculated" checkbox —
    # required when changing scoring config on an ACTIVE campaign.
    confirm_scoring_change: bool = False

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: Optional[str]):
        if value is None:
            return value
        value = value.strip()
        if not value:
            raise ValueError("Campaign name cannot be empty.")
        return value