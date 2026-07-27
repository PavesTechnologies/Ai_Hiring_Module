from typing import Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class SkillSuggestionResponse(BaseModel):
    """
    Shared response DTO for all four Unknown Skill suggestion endpoints
    (RapidFuzz/semantic x canonical/alias). matched_alias is always None
    for canonical-tier endpoints and the matched alias text for alias-tier
    endpoints. similarity is 0-100 for RapidFuzz tiers, 0.0-1.0 for semantic
    tiers - callers already know which endpoint they called.
    """

    model_config = ConfigDict(from_attributes=True)

    skill_id: UUID
    skill_name: str
    matched_alias: Optional[str] = None
    similarity: float
