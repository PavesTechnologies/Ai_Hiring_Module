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

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str):
        value = value.strip()

        if not value:
            raise ValueError("Campaign name cannot be empty.")

        return value