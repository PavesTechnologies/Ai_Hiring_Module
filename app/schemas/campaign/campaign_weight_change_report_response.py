from datetime import datetime
from uuid import UUID

from pydantic import BaseModel


class WeightChangeReportRow(BaseModel):
    campaign_id: UUID
    campaign_name: str
    campaign_status: str
    change_date: datetime
    changed_by: str
    previous_weights: dict
    new_weights: dict
    candidates_processed_with_this_config: int


class WeightChangeReportResponse(BaseModel):
    rows: list[WeightChangeReportRow]
    total_count: int
