from datetime import datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class CampaignWeightPresetResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    description: str | None

    weight_deterministic: Decimal
    weight_semantic: Decimal
    weight_ai: Decimal

    deterministic_threshold: Decimal
    semantic_threshold: Decimal
    ai_threshold: Decimal

    created_by: str
    created_at: datetime


class CampaignWeightPresetCreateRequest(BaseModel):

    name: str = Field(
        ...,
        min_length=1,
        max_length=100,
    )

    description: str | None = Field(
        default=None,
        max_length=255,
    )

    weight_deterministic: Decimal
    weight_semantic: Decimal
    weight_ai: Decimal
    deterministic_threshold: Decimal
    semantic_threshold: Decimal
    ai_threshold: Decimal


class CampaignWeightPresetUpdateRequest(BaseModel):

    name: str = Field(
        ...,
        min_length=1,
        max_length=100,
    )

    description: str | None = Field(
        default=None,
        max_length=255,
    )

    weight_deterministic: Decimal
    weight_semantic: Decimal
    weight_ai: Decimal
    deterministic_threshold: Decimal
    semantic_threshold: Decimal
    ai_threshold: Decimal