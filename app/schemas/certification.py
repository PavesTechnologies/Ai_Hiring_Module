from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


class CertificationSkillItem(BaseModel):
    canonical_skill_id: UUID
    skill: str | None = None
    sense_key: str | None


class CertificationItem(BaseModel):
    id: UUID
    name: str
    name_variants: list[str]
    note: str | None
    skills: list[CertificationSkillItem]
    created_at: datetime


class SenseOptionItem(BaseModel):
    sense_key: str
    label: str


class SkillMeaningsItem(BaseModel):
    canonical_skill_id: UUID
    skill: str | None
    senses: list[SenseOptionItem]


class CertificationCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=150)
    name_variants: list[str] = Field(default_factory=list, max_length=20)
    note: str | None = Field(default=None, max_length=500)


class CertificationSkillCreate(BaseModel):
    canonical_skill_id: UUID
    sense_key: str | None = Field(default=None, max_length=50)
