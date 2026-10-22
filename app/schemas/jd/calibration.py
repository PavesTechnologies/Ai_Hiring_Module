"""JD calibration (Phase 2): the HR_ADMIN review of a JD's scoring inputs."""
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, model_validator

from app.schemas.jd_profile_template import JDProfileTemplateItem, ProfileTemplateKey

SkillTier = Literal["CORE", "SUPPORTING", "PREFERRED"]
EducationRequirement = Literal["MANDATORY", "PREFERRED"]
InternshipCreditPct = Literal[100, 50, 0]
RoleDomainKey = Literal["SOFTWARE", "IT_OPERATIONS", "DATA", "BUSINESS", "OTHER"]


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
    # Core skills only: never allowed missing (the missing-core allowance
    # covers only the other core skills).
    required: bool = False


class SkillSuggestion(BaseModel):
    canonical_skill_id: UUID
    canonical_name: str
    similarity: float


class CalibrationUnknownSkill(BaseModel):
    """A JD skill not in the catalogue: mapped / approved as new / dropped for this JD."""
    unknown_skill_id: UUID
    raw_text: str
    tier: SkillTier | None
    status: str                       # PENDING / RESOLVED
    resolution: str | None = None     # MAPPED / NEW_SKILL / DROPPED (resolved here)
    resolved_canonical_skill_id: UUID | None = None
    resolved_canonical_name: str | None = None
    resolved_by_name: str | None = None
    resolved_at: datetime | None = None
    # An unresolved CORE skill blocks calibration.
    blocks_calibration: bool = False


class UnknownSkillSuggestions(BaseModel):
    """Top-3 catalogue skills by embedding similarity for one pending unknown skill."""
    unknown_skill_id: UUID
    suggestions: list[SkillSuggestion]


class JDUnknownSkillResolveRequest(BaseModel):
    """MAP to a catalogue skill, approve as a NEW catalogue skill (global), or DROP from this JD."""
    action: Literal["MAP", "NEW", "DROP"]
    canonical_skill_id: UUID | None = None          # MAP
    canonical_name: str | None = Field(default=None, max_length=255)   # NEW (default: the raw text)
    aliases: list[str] = []                          # NEW
    category: str | None = None                      # NEW
    parent_skill_id: UUID | None = None              # NEW


class JDCalibrationAddSkillRequest(BaseModel):
    """Add a skill the extraction missed: a catalogue skill, or a new one (added to the catalogue)."""
    canonical_skill_id: UUID | None = None
    new_skill_name: str | None = Field(default=None, min_length=1, max_length=255)
    tier: SkillTier
    sense_key: str | None = Field(default=None, max_length=64)

    @model_validator(mode="after")
    def _one_source(self):
        has_name = bool(self.new_skill_name and self.new_skill_name.strip())
        if (self.canonical_skill_id is None) == (not has_name):
            raise ValueError("Give either canonical_skill_id (a catalogue skill) or new_skill_name, not both.")
        return self


class JDCalibrationSkillChangeResponse(BaseModel):
    action: str                                   # ADDED / REMOVED
    jd_skill_id: UUID
    canonical_skill_id: UUID | None
    skill: str | None
    created_new_skill: bool
    calibration_version: int | None               # None: the JD isn't calibrated yet (no version)
    candidates_marked_out_of_date: int
    calibration: "JDCalibrationView"


class JDUnknownSkillResolveResponse(BaseModel):
    action: str
    unknown_skill_id: UUID
    canonical_skill_id: UUID | None
    # Every JD the decision resolved (NEW is global: all JDs with this text).
    affected_jd_ids: list[UUID]
    candidates_marked_out_of_date: int
    calibration: "JDCalibrationView"


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
    # Internship time counted as experience (%): as calibrated (None = the default),
    # and the default - 100 on a fresher JD (calibrated minimum <= 1 year), else 0.
    internship_credit_pct: InternshipCreditPct | None = None
    default_internship_credit_pct: InternshipCreditPct = 0
    is_fresher_jd: bool = False
    # Role domain as calibrated (None = not set), and the one suggested from the
    # JD's title and core skills (with what it was based on: "title" / "skills").
    role_domain: RoleDomainKey | None = None
    suggested_role_domain: RoleDomainKey | None = None
    suggested_role_domain_basis: str | None = None
    # Profile template (Phase 3 Step 7) as calibrated (None = none), the one
    # suggested from the JD (basis: experience / title / role domain /
    # certifications / default), and the templates with their defaults - the
    # form prefills the settings from the one chosen.
    profile_template: ProfileTemplateKey | None = None
    suggested_profile_template: ProfileTemplateKey | None = None
    suggested_profile_template_basis: str | None = None
    profile_templates: list[JDProfileTemplateItem] = []
    live_campaigns: list[CalibrationCampaign]
    can_calibrate: bool
    warnings: list[str] = []
    # Unresolved CORE unknown skills: calibration is refused until they're resolved.
    blocking_unknown_skills: list[str] = []


class CalibrationSkillInput(BaseModel):
    jd_skill_id: UUID
    tier: SkillTier
    sense_key: str | None = None
    # Only a CORE skill can be required.
    required: bool = False


class JDCalibrationSaveRequest(BaseModel):
    # Every JD skill, each with its tier (and sense when ambiguous).
    skills: list[CalibrationSkillInput] = Field(..., min_length=1)
    min_experience_years: float | None = Field(default=None, ge=0, le=60)
    # None = the default tolerance (max(1 year, 10% of the minimum)).
    experience_tolerance_years: float | None = Field(default=None, ge=0, le=20)
    education_requirement: EducationRequirement = "MANDATORY"
    # None = the default (100 on a fresher JD, else 0).
    internship_credit_pct: InternshipCreditPct | None = None
    # None = not set (no related education fields apply).
    role_domain: RoleDomainKey | None = None
    # None = no template (unset settings use the rules above; standard evidence sections).
    profile_template: ProfileTemplateKey | None = None


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


JDUnknownSkillResolveResponse.model_rebuild()
JDCalibrationSkillChangeResponse.model_rebuild()
