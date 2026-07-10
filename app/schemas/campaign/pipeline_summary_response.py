from uuid import UUID
from pydantic import BaseModel


class StageStat(BaseModel):
    stage: str
    count: int
    drop_off_pct: float | None   # None for the first stage (nothing to drop from)


class PipelineSummaryResponse(BaseModel):
    campaign_id: UUID
    total_candidates: int
    stages: list[StageStat]
