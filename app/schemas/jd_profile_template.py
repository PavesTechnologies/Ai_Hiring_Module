"""JD profile templates (Phase 3 Step 7): Fresher / Experienced / Domain."""
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

ProfileTemplateKey = Literal["FRESHER", "EXPERIENCED", "DOMAIN"]


class JDProfileTemplateItem(BaseModel):
    key: ProfileTemplateKey
    label: str
    description: str | None
    internship_credit_pct: Literal[100, 50, 0]
    experience_tolerance_years: float | None        # None = the system default
    education_requirement: Literal["MANDATORY", "PREFERRED"]
    evidence_sections: list[str]
    # Listed-only core skills can be confirmed by vote (else at most UNCERTAIN).
    listed_only_can_confirm: bool = False
    updated_by: str | None = None
    updated_at: datetime | None = None

    model_config = {"from_attributes": True}

    @field_validator("experience_tolerance_years", mode="before")
    @classmethod
    def _float(cls, value):
        return float(value) if value is not None else None


class JDProfileTemplateList(BaseModel):
    templates: list[JDProfileTemplateItem]
    # What a template can count as evidence (experience always counts), for the editor.
    evidence_section_options: list[str]
    required_evidence_sections: list[str]


class JDProfileTemplateUpdate(BaseModel):
    label: str = Field(..., min_length=1, max_length=60)
    description: str | None = Field(default=None, max_length=500)
    internship_credit_pct: Literal[100, 50, 0]
    experience_tolerance_years: float | None = Field(default=None, ge=0, le=20)
    education_requirement: Literal["MANDATORY", "PREFERRED"]
    evidence_sections: list[str] = Field(..., min_length=1)
    listed_only_can_confirm: bool


class JDProfileTemplateUpdateResponse(BaseModel):
    template: JDProfileTemplateItem
    changed: bool
    candidates_marked_out_of_date: int
