from enum import Enum
from uuid import UUID

from pydantic import BaseModel


class UnknownSkillResolutionType(str, Enum):
    MAP_TO_EXISTING = "MAP_TO_EXISTING"
    ADD_AS_ALIAS = "ADD_AS_ALIAS"


class ResolveUnknownSkillRequest(BaseModel):
    canonical_skill_id: UUID
    type: UnknownSkillResolutionType
