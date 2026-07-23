from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel


class ScoreDistributionResponse(BaseModel):
    has_processed_candidates: bool
    message: str | None = None

    average_composite_score: float | None = None
    median_composite_score: float | None = None
    highest_composite_score: float | None = None
    lowest_composite_score: float | None = None

    passed_all_layers_count: int = 0
    rejected_deterministic_count: int = 0
    rejected_semantic_count: int = 0
    rejected_ai_count: int = 0


class CampaignComparisonColumn(BaseModel):
    campaign_id: UUID
    campaign_name: str
    status: str
    jd_title: str

    weight_deterministic: Decimal
    weight_semantic: Decimal
    weight_ai: Decimal
    semantic_threshold: Decimal
    ai_threshold: Decimal

    total_candidates: int

    score_distribution: ScoreDistributionResponse


class CampaignComparisonResponse(BaseModel):
    campaigns: list[CampaignComparisonColumn]
    # True per field name if that field's value is identical across every
    # compared campaign — lets the frontend render the "Consistent" badge
    # without recomputing the comparison itself.
    consistent_fields: dict[str, bool]
