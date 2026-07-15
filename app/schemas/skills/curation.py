from datetime import datetime
from uuid import UUID

from pydantic import BaseModel


class UnknownSkillItem(BaseModel):
    id: UUID
    raw_text: str
    normalized_key: str | None
    frequency: int
    first_seen: datetime
    last_seen: datetime
    status: str


class MapUnknownSkillRequest(BaseModel):
    target_skill_id: UUID
    save_as_alias: bool = False


class PromoteUnknownSkillRequest(BaseModel):
    category: str | None = None


class RemapJDSkillRequest(BaseModel):
    new_canonical_skill_id: UUID


class UnknownSkillActionResponse(BaseModel):
    id: UUID
    raw_text: str
    status: str


class PromotedSkillResponse(BaseModel):
    id: UUID
    canonical_name: str


class JDSkillRemapResponse(BaseModel):
    id: UUID
    jd_id: UUID
    canonical_skill_id: UUID
    match_tier: str
