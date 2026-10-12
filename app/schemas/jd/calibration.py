"""JD calibration (Phase 2): the HR_ADMIN review of a JD's scoring inputs."""
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

SkillTier = Literal["CORE", "SUPPORTING", "PREFERRED"]
EducationRequirement = Literal["MANDATORY", "PREFERRED"]


class SenseOption(BaseModel):
    sense_key: str
    label: str
    description: str
    is_default: bool


class CalibrationSkill(BaseModel):
    jd_skill_id: UUID
    canonical_skill_id: UUID
    name: str
    tier: SkillTier | None            # None: a required skill never classified (legacy)
    # Ambiguous skills only (2+ senses): the options, the sense HR confirmed
    # (sense_key), and what scoring would use today without it.
    ambiguous: bool = False
    sense_options: list[SenseOption] = []
    sense_key: str | None = None
    suggested_sense_key: str | None = None
    suggested_sense_basis: str | None = None   # cue / title / assumed


class CalibrationUnknownSkill(BaseModel):
    """A JD skill not in the catalogue (resolved in the unknown-skills flow)."""
    unknown_skill_id: UUID
    raw_text: str
    tier: SkillTier | None
    status: str                       # PENDING / RESOLVED


class CalibrationCampaign(BaseModel):
    campaign_id: UUID
    name: str
    status: str


class JDCalibrationView(BaseModel):
    jd_id: UUID
    title: str
    version_number: int
    calibration_status: str           # NOT_CALIBRATED / CALIBRATED - the main readiness status
    calibration_version: int
    calibrated_by: str | None
    calibrated_by_name: str | None
    calibrated_at: datetime | None
    # Detail inside calibration: unknown-skill resolution (is_verified).
    verification_status: str
    pending_unknown_skill_count: int
    skills: list[CalibrationSkill]
    unknown_skills: list[CalibrationUnknownSkill]
    core_skill_count: int
    min_experience_years: float | None
    # The extracted minimum (extraction, else column) - what a blank calibrated minimum falls back to.
    extracted_min_experience_years: float | None = None
    experience_tolerance_years: float | None          # as calibrated; None = default
    default_experience_tolerance_years: float | None  # max(1 year, 10% of the minimum)
    education_requirement: EducationRequirement
    education_degree: str | None
    education_field: str | None
    live_campaigns: list[CalibrationCampaign]
    can_calibrate: bool
    warnings: list[str] = []


class CalibrationSkillInput(BaseModel):
    jd_skill_id: UUID
    tier: SkillTier
    sense_key: str | None = None


class JDCalibrationSaveRequest(BaseModel):
    # Every JD skill, each with its tier (and sense when ambiguous).
    skills: list[CalibrationSkillInput] = Field(..., min_length=1)
    min_experience_years: float | None = Field(default=None, ge=0, le=60)
    # None = the default tolerance (max(1 year, 10% of the minimum)).
    experience_tolerance_years: float | None = Field(default=None, ge=0, le=20)
    education_requirement: EducationRequirement = "MANDATORY"


class JDCalibrationSaveResponse(BaseModel):
    changed: bool
    calibration_version: int
    candidates_marked_out_of_date: int
    warnings: list[str] = []
    calibration: JDCalibrationView


class JDCalibrationVersionItem(BaseModel):
    version_no: int
    calibrated_by: str
    calibrated_by_name: str | None
    calibrated_at: datetime
    changes: dict | None
