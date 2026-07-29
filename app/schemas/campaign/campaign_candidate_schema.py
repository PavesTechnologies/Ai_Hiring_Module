from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator
from datetime import date, datetime

from app.models.pipeline import PipelineStage, RejectionLayer

class CampaignCandidateCreateRequest(BaseModel):
    campaign_id: UUID
    candidate_id: UUID
    resume_id: UUID

    model_config = ConfigDict(
        from_attributes=True,
    )

class CampaignCandidateResponse(BaseModel):
    id: UUID
    campaign_id: UUID
    candidate_id: UUID
    # Same value as `id` - kept as its own named field since the Candidate
    # Listing UI refers to it by this name specifically. `id` is preserved
    # unchanged for existing consumers (e.g. create_campaign_candidate).
    campaign_candidate_id: UUID | None = None
    resume_id: UUID

    pipeline_stage: PipelineStage

    # Candidate Listing UI fields (M03-E05-adjacent listing extension).
    # All read-only, sourced from existing stored data - never recalculated.
    candidate_name: str | None = None
    current_designation: str | None = None
    experience: float | None = None

    deterministic_score: float | None = None
    ai_ats_score: float | None = None
    semantic_score: float | None = None
    composite_score: float | None = None

    # Not available in the backend today - always null until a real source exists.
    location: str | None = None
    risk_score: float | None = None

    created_at: datetime

    model_config = ConfigDict(
        from_attributes=True,
    )


class DeterministicScoreSummary(BaseModel):
    """UI summary-card fields - all read straight from score_breakdown/CampaignCandidate, never recalculated."""

    overall_score: float | None = None
    status: str | None = None
    threshold: float | None = None
    mandatory_coverage_pct: float | None = None
    mandatory_skills_matched: int | None = None
    mandatory_skills_total: int | None = None
    preferred_skills_matched: int | None = None
    preferred_skills_total: int | None = None
    # Not tracked in score_breakdown today (would require a fresh
    # candidate-skills query, not a transform of already-stored data) -
    # always null, per "if a value is unavailable, return null instead of
    # recalculating".
    additional_skills_count: int | None = None
    experience_status: str | None = None
    education_status: str | None = None
    screened_at: datetime | None = None

    # Reuses candidate_rejections.rejection_reason (via the scorecard's own
    # rejection banner) - never a second/independent evaluation. null when
    # the candidate has no deterministic rejection on record.
    failure_reason: str | None = None
    # Same text as failure_reason, split on " | " - the exact delimiter
    # CandidateScoringService.build_rejection_reason already concatenates
    # multiple failure clauses with. [] when failure_reason is null.
    failure_reasons: list[str] = []
    # Same underlying value as screened_at above (kept for backward
    # compatibility) under the name this contract asks for.
    screening_completed_at: datetime | None = None


class MissingMandatorySkillItem(BaseModel):
    skill: str | None = None
    configured_weight: float | None = None
    reason: str | None = None


class MandatorySkillBreakdownItem(BaseModel):
    jd_skill: str | None = None
    candidate_skill: str | None = None
    mandatory: bool | None = None
    match_type: str | None = None
    configured_weight: float | None = None
    # candidate_scoring_weight from the skill-normalization step (e.g. a
    # FUZZY/SEMANTIC text match is scored below 1.0) - named for the UI's
    # "normalization discount" column.
    normalization_discount: float | None = None
    hierarchy_multiplier: float | None = None
    contribution: float | None = None
    confidence: float | None = None
    passed: bool | None = None
    # matched is the same value as passed above, under the name this
    # contract asks for - kept as a separate field rather than renaming
    # passed, to preserve it unchanged for existing consumers.
    matched: bool | None = None
    match_reason: str | None = None
    # (contribution / configured_weight) x 100 - null if weight is missing/zero.
    contribution_percentage: float | None = None


class PreferredSkillBreakdownItem(BaseModel):
    jd_skill: str | None = None
    candidate_skill: str | None = None
    match_type: str | None = None
    configured_weight: float | None = None
    bonus: float | None = None
    confidence: float | None = None
    matched: bool | None = None
    match_reason: str | None = None
    contribution_percentage: float | None = None


class AdditionalCandidateSkillItem(BaseModel):
    """Reserved for a future candidate-skills diff - always empty today (see summary.additional_skills_count)."""

    skill: str | None = None
    confidence: float | None = None


class HierarchyMatchItem(BaseModel):
    jd_skill: str | None = None
    candidate_skill: str | None = None
    relationship: str | None = None
    multiplier: float | None = None
    # match_type/hierarchy_multiplier duplicate relationship/multiplier
    # above under the names this contract asks for - relationship/
    # multiplier are kept unchanged for existing consumers.
    match_type: str | None = None
    hierarchy_multiplier: float | None = None


class ExperienceValidationDetail(BaseModel):
    required_years: float | None = None
    candidate_years: float | None = None
    tolerance: float | None = None
    passed: bool | None = None
    # PASSED / FAILED / DATA_MISSING / SKIPPED / NOT_APPLICABLE - mapped
    # straight from the stored applicable/skipped/data_missing/passed
    # flags, never recalculated. "SKIPPED" matches this codebase's own
    # existing terminology (ExperienceEducationValidationService's own
    # docstring already calls the no-JD-requirement case "SKIPPED").
    status: str | None = None


class EducationValidationDetail(BaseModel):
    required_degree: str | None = None
    candidate_degree: str | None = None
    equivalent_experience_applied: bool | None = None
    passed: bool | None = None


class ScoreCalculationDetail(BaseModel):
    skills_score: float | None = None
    experience_score: float | None = None
    education_score: float | None = None
    final_score: float | None = None


class ScoreConfigurationDetail(BaseModel):
    skills_weight: float | None = None
    experience_weight: float | None = None
    education_weight: float | None = None
    deterministic_threshold: float | None = None
    semantic_threshold: float | None = None
    hierarchy_grandchild_multiplier: float | None = None
    # CHILD/SIBLING/SEMANTIC multipliers are hardcoded literals in
    # CandidateScoringService today (0.7/0.4/0.2), not PlatformConfig keys -
    # read-attempted here (forward-compatible if they're ever moved into
    # PlatformConfig) but null under the current system, per "read only
    # from existing PlatformConfig, return null if unavailable".
    hierarchy_child_multiplier: float | None = None
    hierarchy_sibling_multiplier: float | None = None
    semantic_multiplier: float | None = None


class DeterministicScoreBreakdownResponse(BaseModel):
    """
    UI-friendly restructuring of CampaignCandidate.score_breakdown (+
    screened_at, + a handful of PlatformConfig display values) - every
    field here is a direct read or a pure display-formatting transform of
    already-computed/stored data, nothing is recalculated. None whenever
    score_breakdown itself doesn't exist yet (scoring hasn't run).
    """

    summary: DeterministicScoreSummary
    missing_mandatory_skills: list[MissingMandatorySkillItem]
    mandatory_skills: list[MandatorySkillBreakdownItem]
    preferred_skills: list[PreferredSkillBreakdownItem]
    additional_candidate_skills: list[AdditionalCandidateSkillItem]
    hierarchy_matches: list[HierarchyMatchItem]
    experience_validation: ExperienceValidationDetail
    education_validation: EducationValidationDetail
    score_calculation: ScoreCalculationDetail
    configuration: ScoreConfigurationDetail

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "summary": {
                    "overall_score": 78.5,
                    "status": "PASSED",
                    "threshold": 70.0,
                    "mandatory_coverage_pct": 83.33,
                    "mandatory_skills_matched": 5,
                    "mandatory_skills_total": 6,
                    "preferred_skills_matched": 2,
                    "preferred_skills_total": 3,
                    "additional_skills_count": None,
                    "experience_status": "PASSED",
                    "education_status": "PASSED",
                    "screened_at": "2026-07-24T10:15:00Z",
                    "failure_reason": None,
                    "failure_reasons": [],
                    "screening_completed_at": "2026-07-24T10:15:00Z",
                },
                "missing_mandatory_skills": [
                    {"skill": "Kubernetes", "configured_weight": 50.0, "reason": "No matching skill found in candidate's profile (including hierarchy fallback)."}
                ],
                "mandatory_skills": [
                    {
                        "jd_skill": "Python", "candidate_skill": "Python", "mandatory": True,
                        "match_type": "EXACT", "configured_weight": 50.0, "normalization_discount": 1.0,
                        "hierarchy_multiplier": 1.0, "contribution": 50.0, "confidence": 1.0, "passed": True,
                        "matched": True, "match_reason": "Exact match with candidate skill 'Python'.",
                        "contribution_percentage": 100.0,
                    }
                ],
                "preferred_skills": [
                    {
                        "jd_skill": "Docker", "candidate_skill": "Docker", "match_type": "EXACT",
                        "configured_weight": 20.0, "bonus": 20.0, "confidence": 1.0,
                        "matched": True, "match_reason": "Exact match with candidate skill 'Docker'.",
                        "contribution_percentage": 100.0,
                    }
                ],
                "additional_candidate_skills": [],
                "hierarchy_matches": [
                    {
                        "jd_skill": "Cloud Computing", "candidate_skill": "AWS", "relationship": "CHILD",
                        "multiplier": 0.7, "match_type": "CHILD", "hierarchy_multiplier": 0.7,
                    }
                ],
                "experience_validation": {
                    "required_years": 5.0, "candidate_years": 4.5, "tolerance": 0.0, "passed": False,
                    "status": "FAILED",
                },
                "education_validation": {"required_degree": "Bachelor's", "candidate_degree": "Master's", "equivalent_experience_applied": False, "passed": True},
                "score_calculation": {"skills_score": 82.0, "experience_score": 90.0, "education_score": 100.0, "final_score": 78.5},
                "configuration": {
                    "skills_weight": 0.70, "experience_weight": 0.15, "education_weight": 0.15,
                    "deterministic_threshold": 70.0, "semantic_threshold": 0.75, "hierarchy_grandchild_multiplier": 0.5,
                    "hierarchy_child_multiplier": None, "hierarchy_sibling_multiplier": None, "semantic_multiplier": None,
                },
            }
        }
    )


# ----------------------------------------------------------------------
# Candidate Scorecard tab endpoints - dedicated, smaller response shapes
# per tab, alongside (never replacing) the full CandidateScorecardResponse
# aggregate below. Each one is built from the exact same shared mapper
# helpers CandidateScorecardResponse itself uses - never a second,
# independent computation.
#
# Future tabs (not implemented yet, per this story's explicit scope):
# GET .../resume, GET .../semantic, GET .../ai-evaluation,
# GET .../final-status - each would follow this same pattern: its own
# small response schema + its own get_candidate_<tab>() service method,
# reusing whatever mapper helpers already exist rather than recomputing.
# ----------------------------------------------------------------------


class AiSummaryDetail(BaseModel):
    """
    Reads CampaignCandidate.ai_recommendation/ai_strengths/ai_weaknesses
    as-is - columns that already exist on the model but are never written
    by anything today (M09 AI Evaluation isn't built), so this is null in
    practice until that epic lands. Never recalculated/derived.
    """

    recommendation: str | None = None
    strengths: dict | list | None = None
    weaknesses: dict | list | None = None


class CandidateSummaryResponse(BaseModel):
    """
    Summary-tab-only view of the candidate scorecard - header, candidate
    info, overall scores, and AI summary (if available). Deliberately
    excludes score_breakdown/deterministic_score_breakdown (the
    Deterministic tab's concern), and the rejection/override banner
    (the future Final Status tab's concern) - each lives in its own
    dedicated response instead.
    """

    # Header information
    campaign_candidate_id: UUID
    campaign_id: UUID
    candidate_id: UUID
    pipeline_stage: PipelineStage
    created_at: datetime

    # Candidate information / details
    candidate_name: str | None = None
    current_designation: str | None = None
    experience: float | None = None
    location: str | None = None

    # Overall scores
    deterministic_score: float | None = None
    ai_ats_score: float | None = None
    semantic_score: float | None = None
    composite_score: float | None = None

    # AI summary, if available - null until M09 AI Evaluation is built.
    ai_summary: AiSummaryDetail | None = None

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "campaign_candidate_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
                "campaign_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
                "candidate_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
                "pipeline_stage": "SCREENING",
                "created_at": "2026-07-20T09:00:00Z",
                "candidate_name": "Jordan Lee",
                "current_designation": "Backend Engineer",
                "experience": 4.5,
                "location": None,
                "deterministic_score": 78.5,
                "ai_ats_score": None,
                "semantic_score": None,
                "composite_score": None,
                "ai_summary": None,
            }
        }
    )


class CandidateDeterministicResponse(BaseModel):
    """
    Deterministic-tab-only view of the candidate scorecard. Reuses
    DeterministicScoreBreakdownResponse exactly as-is (the same object
    CandidateScorecardResponse.deterministic_score_breakdown carries) -
    never a second/independent computation of any deterministic section.
    """

    campaign_candidate_id: UUID
    deterministic_score: float | None = None
    deterministic_score_breakdown: DeterministicScoreBreakdownResponse | None = None

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "campaign_candidate_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
                "deterministic_score": 78.5,
                "deterministic_score_breakdown": "See DeterministicScoreBreakdownResponse's own example.",
            }
        }
    )


class SemanticScoreSummary(BaseModel):
    """
    Mirrors DeterministicScoreSummary's shape (overall_score/status/
    threshold/screened_at) for the semantic layer - same field names where
    the concept is identical, so a frontend already rendering the
    Deterministic tab's summary can reuse the same layout for Semantic.
    """

    overall_score: float | None = None
    status: str | None = None
    threshold: float | None = None
    matching_skills_count: int | None = None
    missing_skills_count: int | None = None
    matched_keywords_count: int | None = None
    screened_at: datetime | None = None
    failure_reason: str | None = None


class SemanticScoreBreakdownResponse(BaseModel):
    """
    M08-E02: pure read/transform of campaign_candidates.semantic_score_breakdown
    (built by SemanticScoringService, never recalculated here) - the
    semantic-layer analog of DeterministicScoreBreakdownResponse.
    """

    summary: SemanticScoreSummary
    overall_similarity: float | None = None
    semantic_passed: bool | None = None
    semantic_threshold: float | None = None
    matching_skills: list[str] = Field(default_factory=list)
    missing_skills: list[str] = Field(default_factory=list)
    matched_keywords: list[str] = Field(default_factory=list)
    semantic_explanation: str | None = None

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "summary": {
                    "overall_score": 0.812345,
                    "status": "PASSED",
                    "threshold": 0.65,
                    "matching_skills_count": 5,
                    "missing_skills_count": 1,
                    "matched_keywords_count": 4,
                    "screened_at": "2026-07-29T10:15:00Z",
                    "failure_reason": None,
                },
                "overall_similarity": 0.812345,
                "semantic_passed": True,
                "semantic_threshold": 0.65,
                "matching_skills": ["Python", "SQL", "Docker"],
                "missing_skills": ["Kubernetes"],
                "matched_keywords": ["python", "sql", "docker", "aws"],
                "semantic_explanation": (
                    "Resume-to-job semantic similarity is 81.2%, which meets the "
                    "configured threshold of 65.0%."
                ),
            }
        }
    )


class CandidateSemanticResponse(BaseModel):
    """
    Semantic-tab-only view of the candidate scorecard. Mirrors
    CandidateDeterministicResponse exactly: campaign_candidate_id +
    semantic_score (the same scalar CampaignCandidateResponse.semantic_score
    already carries) + semantic_score_breakdown, reusing
    _build_semantic_score_breakdown as-is - never a second/independent
    computation. Never includes summary/resume/deterministic/AI-evaluation/
    final-status data.
    """

    campaign_candidate_id: UUID
    semantic_score: float | None = None
    semantic_score_breakdown: SemanticScoreBreakdownResponse | None = None

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "campaign_candidate_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
                "semantic_score": 0.812345,
                "semantic_score_breakdown": "See SemanticScoreBreakdownResponse's own example.",
            }
        }
    )


class CandidateScorecardResponse(CampaignCandidateResponse):
    """
    M07-E03 S03 T01: extends CampaignCandidateResponse (never duplicates
    it) with the rejection banner - used only by the single-candidate
    scorecard detail endpoint, never the campaign candidate list, so list
    consumers are entirely unaffected.

    has_rejection is only ever True when pipeline_stage == REJECTED AND
    the candidate's most recent candidate_rejections row is
    rejection_layer == DETERMINISTIC (this story's exact, explicit scope -
    a SEMANTIC/AI-layer rejection is a different epic, not surfaced here).
    """
    has_rejection: bool = False
    rejection_layer: RejectionLayer | None = None
    rejection_reason: str | None = None
    rejected_at: datetime | None = None
    score_breakdown: dict | None = None

    # Present regardless of has_rejection - hr_override can in principle
    # be set independently of this story's DETERMINISTIC-only banner scope.
    is_overridden: bool = False
    # "Overridden — Previously Rejected" when is_overridden, else None -
    # the original rejection_reason/rejected_at above are preserved
    # unchanged either way, never overwritten by the override.
    status: str | None = None

    # New, additive field: a UI-friendly restructuring of the existing
    # score_breakdown above - score_breakdown itself is untouched/unchanged
    # for any existing consumer. None until scoring has actually run.
    deterministic_score_breakdown: DeterministicScoreBreakdownResponse | None = None


class CandidateRejectionHistoryEntryResponse(BaseModel):
    """M07-E03 S03 T02: one candidate_rejections row, read-only - no edit/delete APIs exist or are added."""
    id: UUID
    rejection_layer: RejectionLayer
    rejection_reason: str
    rejected_at: datetime
    hr_override: bool
    # 1-indexed, oldest=1 - position among this candidate's own rejection
    # history, not a stored column (candidate_rejections has no such
    # field); computed purely from rejected_at ordering.
    evaluation_round: int
    # True only for the single newest record in the list.
    current_status: bool

    model_config = ConfigDict(
        from_attributes=True,
    )


class HrOverrideRequest(BaseModel):
    """M07-E03 S04 T01: HR_ADMIN override of a deterministic rejection."""

    override_reason: str = Field(..., min_length=20)
    confirmation: bool

    @field_validator("confirmation")
    @classmethod
    def _confirmation_must_be_true(cls, value: bool) -> bool:
        if not value:
            raise ValueError("confirmation must be true to apply an HR override.")
        return value


class OverrideReportRow(BaseModel):
    """M07-E03 S04 T03: one HR override event - never includes candidate name/email/phone/resume."""

    campaign_id: UUID
    campaign_name: str
    candidate_uuid: UUID
    original_rejection_reason: str | None = None
    override_reason: str
    hr_full_name: str | None = None
    override_timestamp: datetime
    current_pipeline_stage: PipelineStage

    model_config = ConfigDict(
        from_attributes=True,
    )


class OverrideWeeklyTrendPoint(BaseModel):
    """One week's override count - Monday-anchored week_start, last 8 weeks."""

    week_start: date
    override_count: int


class CampaignOverrideAlert(BaseModel):
    """
    override_rate = overrides / rejected candidates in this campaign (%),
    all-time (not scoped to the report's date-range filter, which only
    scopes `rows`). override_alert is True when override_rate exceeds
    the OVERRIDE_RATE_ALERT_THRESHOLD platform_config key.
    """

    campaign_id: UUID
    campaign_name: str
    override_count: int
    rejected_count: int
    override_rate: float
    override_alert: bool
    recommendation: str | None = None


class OverrideReportResponse(BaseModel):
    rows: list[OverrideReportRow]
    total_count: int
    weekly_trend: list[OverrideWeeklyTrendPoint]
    campaign_alerts: list[CampaignOverrideAlert]


class RejectionBreakdownEntry(BaseModel):
    """M07-E03 S05 T01: one of the 7 mandatory/experience/education failure-combination buckets."""

    category: str
    count: int
    percentage: float


class MissingSkillOccurrence(BaseModel):
    """M07-E03 S05 T01: one canonical skill's occurrence among MISSING mandatory-skill matches."""

    canonical_name: str
    occurrence_count: int
    percentage_of_rejections: float


class JdCalibrationRecommendation(BaseModel):
    """M07-E03 S05 T02: one structured JD-calibration suggestion."""

    rule: str
    message: str
    action: str | None = None
    details: dict | None = None


class CampaignRejectionAnalyticsResponse(BaseModel):
    campaign_id: UUID
    total_candidates: int
    total_deterministic_rejections: int
    # The threshold actually used to gate `recommendations` below - read
    # from PlatformConfig, never hardcoded (M07-E03 S05 T02).
    min_candidates_for_analytics: int
    breakdown: list[RejectionBreakdownEntry]
    top_missing_skills: list[MissingSkillOccurrence]


class ResubmissionInfoResponse(BaseModel):
    """
    Epic 3 (M05-E03) Phase C5 — attached to the existing "candidate already
    exists in this campaign" 409's `data` field (CampaignException itself
    is unchanged - same status code, same message, same behavior for every
    existing caller).
    """
    campaign_candidate_id: UUID
    current_pipeline_stage: PipelineStage
    current_resume_id: UUID
    can_update_resume: bool
    requires_hr_confirmation: bool


class UpdateResumeResubmissionResponse(BaseModel):
    campaign_candidate: CampaignCandidateResponse
    new_resume_id: UUID
    task_id: UUID


class CandidateCampaignHistoryEntryResponse(BaseModel):
    """Epic 3 (M05-E03) Phase C6 — one campaign a candidate has participated in, most recent first."""

    campaign_candidate_id: UUID
    campaign_id: UUID
    campaign_name: str
    jd_title: str
    submission_date: datetime
    pipeline_stage: PipelineStage
    composite_score: float | None
    # Derived: "Selected" / "Rejected" / "In Progress" - never a raw enum value,
    # kept distinct from pipeline_stage per the C6 spec's separate field naming.
    outcome: str


class CandidateCampaignHistoryResponse(BaseModel):
    candidate_id: UUID
    total_campaigns: int
    history: list[CandidateCampaignHistoryEntryResponse]