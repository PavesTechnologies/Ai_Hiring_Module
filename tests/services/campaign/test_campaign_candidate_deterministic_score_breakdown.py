from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

from app.schemas.campaign.campaign_candidate_schema import DeterministicScoreBreakdownResponse
from app.services.campaign.campaign_candidate_service import CampaignCandidateService

"""
Deterministic Score API response contract - deterministic_score_breakdown
is a pure UI-friendly restructuring of the already-computed/stored
score_breakdown JSONB. These tests verify the mapping is correct and that
nothing is recalculated - every assertion traces back to a literal input
value, never a freshly-derived business decision.
"""


def _make_campaign_candidate(score_breakdown=None, screened_at=None):
    return SimpleNamespace(
        id=uuid4(),
        score_breakdown=score_breakdown,
        screened_at=screened_at,
    )


def _skill_entry(
    canonical_name, match_type, configured_weight=50.0, candidate_scoring_weight=1.0,
    hierarchy_score_multiplier=1.0, contribution=50.0, confidence=1.0,
    matched_candidate_skill_canonical_name=None, mandatory=True,
):
    return {
        "canonical_skill_id": str(uuid4()),
        "canonical_name": canonical_name,
        "mandatory": mandatory,
        "configured_weight": configured_weight,
        "match_type": match_type,
        "matched_candidate_skill_canonical_name": matched_candidate_skill_canonical_name,
        "hierarchy_score_multiplier": hierarchy_score_multiplier,
        "candidate_scoring_weight": candidate_scoring_weight,
        "match_tier": "EXACT",
        "confidence": confidence,
        "skill_contribution": contribution,
    }


def make_service(config_repo=None):
    return CampaignCandidateService(
        campaign_repo=MagicMock(),
        campaign_candidate_repo=MagicMock(),
        audit_service=MagicMock(),
        config_repo=config_repo,
    )


def test_returns_none_when_score_breakdown_not_yet_computed():
    service = make_service()
    candidate = _make_campaign_candidate(score_breakdown=None)

    assert service._build_deterministic_score_breakdown(candidate) is None


def test_returns_none_when_score_breakdown_is_empty_dict():
    service = make_service()
    candidate = _make_campaign_candidate(score_breakdown={})

    assert service._build_deterministic_score_breakdown(candidate) is None


def test_full_breakdown_maps_every_section_from_stored_values():
    screened_at = datetime.now(timezone.utc)
    breakdown = {
        "deterministic_score": 78.5,
        "deterministic_passed": True,
        "deterministic_threshold": 70.0,
        "mandatory_coverage_pct": 83.33,
        "skill_deterministic_score": 82.0,
        "mandatory_skills": [
            _skill_entry("Python", "EXACT", matched_candidate_skill_canonical_name="Python"),
            _skill_entry(
                "Cloud Computing", "CHILD", configured_weight=30.0, candidate_scoring_weight=0.9,
                hierarchy_score_multiplier=0.7, contribution=18.9,
                matched_candidate_skill_canonical_name="AWS",
            ),
            _skill_entry(
                "Kubernetes", "MISSING", configured_weight=20.0, candidate_scoring_weight=None,
                hierarchy_score_multiplier=0.0, contribution=0.0, confidence=None,
            ),
        ],
        "preferred_skills": [
            _skill_entry("Docker", "EXACT", configured_weight=20.0, contribution=20.0, matched_candidate_skill_canonical_name="Docker", mandatory=False),
            _skill_entry("Terraform", "MISSING", configured_weight=10.0, candidate_scoring_weight=None, contribution=0.0, confidence=None, mandatory=False),
        ],
        "experience_validation": {
            "applicable": True, "skipped": False, "data_missing": False, "passed": False,
            "score": 90.0, "candidate_years": 4.5, "min_years": 5.0, "effective_min_years": 5.0,
        },
        "education_validation": {
            "applicable": True, "skipped": False, "data_missing": False, "passed": True,
            "score": 100.0, "required_level": "BACHELOR", "candidate_level": "MASTER",
            "equivalent_experience_applied": False,
        },
    }
    candidate = _make_campaign_candidate(score_breakdown=breakdown, screened_at=screened_at)

    config_repo = MagicMock()
    config_repo.get_configs_by_keys.return_value = {
        "DETERMINISTIC_WEIGHT_SKILLS": "0.70",
        "DETERMINISTIC_WEIGHT_EXPERIENCE": "0.15",
        "DETERMINISTIC_WEIGHT_EDUCATION": "0.15",
        "HIERARCHY_SEMANTIC_ONLY_THRESHOLD": "0.75",
        "HIERARCHY_GRANDCHILD_MULTIPLIER": "0.5",
    }
    service = make_service(config_repo=config_repo)

    result = service._build_deterministic_score_breakdown(
        candidate, rejection_reason="Missing required skills: Kubernetes. | Insufficient experience: 4.5 years provided, minimum 5 years required (gap: 0.5 years).",
    )

    assert isinstance(result, DeterministicScoreBreakdownResponse)

    # summary
    assert result.summary.overall_score == 78.5
    assert result.summary.status == "PASSED"
    assert result.summary.threshold == 70.0
    assert result.summary.mandatory_coverage_pct == 83.33
    assert result.summary.mandatory_skills_matched == 2  # EXACT + CHILD, not MISSING
    assert result.summary.mandatory_skills_total == 3
    assert result.summary.preferred_skills_matched == 1
    assert result.summary.preferred_skills_total == 2
    assert result.summary.additional_skills_count is None
    assert result.summary.experience_status == "FAILED"
    assert result.summary.education_status == "PASSED"
    assert result.summary.screened_at == screened_at
    assert result.summary.screening_completed_at == screened_at
    assert result.summary.failure_reason == (
        "Missing required skills: Kubernetes. | Insufficient experience: 4.5 years provided, minimum 5 years required (gap: 0.5 years)."
    )
    assert result.summary.failure_reasons == [
        "Missing required skills: Kubernetes.",
        "Insufficient experience: 4.5 years provided, minimum 5 years required (gap: 0.5 years).",
    ]

    # missing_mandatory_skills
    assert len(result.missing_mandatory_skills) == 1
    assert result.missing_mandatory_skills[0].skill == "Kubernetes"
    assert result.missing_mandatory_skills[0].configured_weight == 20.0
    assert result.missing_mandatory_skills[0].reason

    # mandatory_skills
    assert len(result.mandatory_skills) == 3
    python_entry = result.mandatory_skills[0]
    assert python_entry.jd_skill == "Python"
    assert python_entry.candidate_skill == "Python"
    assert python_entry.match_type == "EXACT"
    assert python_entry.passed is True
    assert python_entry.matched is True
    assert python_entry.match_reason == "Exact match with candidate skill 'Python'."
    assert python_entry.contribution_percentage == 100.0  # 50 / 50 * 100
    kubernetes_entry = result.mandatory_skills[2]
    assert kubernetes_entry.match_type == "MISSING"
    assert kubernetes_entry.passed is False
    assert kubernetes_entry.matched is False
    assert kubernetes_entry.match_reason == "No matching skill found in candidate's profile (including hierarchy fallback)."
    assert kubernetes_entry.contribution_percentage == 0.0  # 0 / 20 * 100
    cloud_entry = result.mandatory_skills[1]
    assert cloud_entry.normalization_discount == 0.9
    assert cloud_entry.hierarchy_multiplier == 0.7
    assert cloud_entry.contribution == 18.9
    assert cloud_entry.matched is True
    assert cloud_entry.match_reason == "Matched via a related child skill 'AWS'."
    assert cloud_entry.contribution_percentage == 63.0  # 18.9 / 30 * 100

    # preferred_skills
    assert len(result.preferred_skills) == 2
    assert result.preferred_skills[0].bonus == 20.0
    assert result.preferred_skills[0].matched is True
    assert result.preferred_skills[0].contribution_percentage == 100.0
    assert result.preferred_skills[1].match_type == "MISSING"
    assert result.preferred_skills[1].matched is False

    # additional_candidate_skills - always empty, not fabricated
    assert result.additional_candidate_skills == []

    # hierarchy_matches - only non-EXACT, non-MISSING entries, from both
    # mandatory and preferred mappings
    assert len(result.hierarchy_matches) == 1
    assert result.hierarchy_matches[0].jd_skill == "Cloud Computing"
    assert result.hierarchy_matches[0].candidate_skill == "AWS"
    assert result.hierarchy_matches[0].relationship == "CHILD"
    assert result.hierarchy_matches[0].multiplier == 0.7
    assert result.hierarchy_matches[0].match_type == "CHILD"
    assert result.hierarchy_matches[0].hierarchy_multiplier == 0.7

    # experience_validation
    assert result.experience_validation.required_years == 5.0
    assert result.experience_validation.candidate_years == 4.5
    assert result.experience_validation.tolerance == 0.0  # min_years - effective_min_years
    assert result.experience_validation.passed is False
    assert result.experience_validation.status == "FAILED"

    # education_validation - degree codes transformed to display names
    assert result.education_validation.required_degree == "Bachelor's"
    assert result.education_validation.candidate_degree == "Master's"
    assert result.education_validation.equivalent_experience_applied is False
    assert result.education_validation.passed is True

    # score_calculation
    assert result.score_calculation.skills_score == 82.0
    assert result.score_calculation.experience_score == 90.0
    assert result.score_calculation.education_score == 100.0
    assert result.score_calculation.final_score == 78.5

    # configuration - read from PlatformConfig, not hardcoded
    assert result.configuration.skills_weight == 0.70
    assert result.configuration.experience_weight == 0.15
    assert result.configuration.education_weight == 0.15
    assert result.configuration.deterministic_threshold == 70.0
    assert result.configuration.semantic_threshold == 0.75
    assert result.configuration.hierarchy_grandchild_multiplier == 0.5
    # Not seeded in PlatformConfig (hardcoded literals in the scoring
    # service) - null, never fabricated.
    assert result.configuration.hierarchy_child_multiplier is None
    assert result.configuration.hierarchy_sibling_multiplier is None
    assert result.configuration.semantic_multiplier is None


def test_failure_reason_is_null_when_candidate_has_no_rejection():
    breakdown = {
        "deterministic_score": 90.0, "deterministic_passed": True, "deterministic_threshold": 70.0,
        "mandatory_coverage_pct": 100.0, "mandatory_skills": [], "preferred_skills": [],
    }
    candidate = _make_campaign_candidate(score_breakdown=breakdown)
    service = make_service()

    result = service._build_deterministic_score_breakdown(candidate, rejection_reason=None)

    assert result.summary.failure_reason is None
    assert result.summary.failure_reasons == []


def test_contribution_percentage_is_null_when_configured_weight_is_zero():
    service = make_service()

    assert service._contribution_percentage(10.0, 0.0) is None
    assert service._contribution_percentage(10.0, None) is None
    assert service._contribution_percentage(None, 50.0) is None
    assert service._contribution_percentage(25.0, 50.0) == 50.0


def test_detailed_validation_status_uses_skipped_not_not_required():
    service = make_service()

    assert service._detailed_validation_status(None) is None
    assert service._detailed_validation_status({"skipped": True, "data_missing": False, "passed": True}) == "SKIPPED"
    assert service._detailed_validation_status({"skipped": False, "data_missing": True, "passed": True}) == "DATA_MISSING"
    assert service._detailed_validation_status({"skipped": False, "data_missing": False, "passed": True}) == "PASSED"
    assert service._detailed_validation_status({"skipped": False, "data_missing": False, "passed": False}) == "FAILED"


def test_skill_match_reason_covers_every_match_type():
    service = make_service()

    assert service._skill_match_reason(None, None) is None
    assert service._skill_match_reason("MISSING", None) == "No matching skill found in candidate's profile (including hierarchy fallback)."
    assert service._skill_match_reason("EXACT", "Python") == "Exact match with candidate skill 'Python'."
    assert service._skill_match_reason("EXACT", None) == "Exact match."
    assert service._skill_match_reason("CHILD", "AWS") == "Matched via a related child skill 'AWS'."
    assert service._skill_match_reason("GRANDCHILD", "Terraform") == "Matched via a related grandchild skill 'Terraform'."
    assert service._skill_match_reason("SIBLING", "GCP") == "Matched via a related sibling skill 'GCP'."
    assert service._skill_match_reason("SEMANTIC", "Ansible") == "Matched via a semantically similar skill 'Ansible'."


def test_configuration_is_null_when_config_repo_not_wired():
    breakdown = {
        "deterministic_score": 50.0, "deterministic_passed": False, "deterministic_threshold": 70.0,
        "mandatory_coverage_pct": 50.0, "mandatory_skills": [], "preferred_skills": [],
    }
    candidate = _make_campaign_candidate(score_breakdown=breakdown)
    service = make_service(config_repo=None)

    result = service._build_deterministic_score_breakdown(candidate)

    assert result.configuration.skills_weight is None
    assert result.configuration.semantic_threshold is None
    assert result.configuration.hierarchy_grandchild_multiplier is None
    # deterministic_threshold still comes from score_breakdown itself, not config.
    assert result.configuration.deterministic_threshold == 70.0


def test_experience_and_education_validation_null_when_not_present_in_breakdown():
    breakdown = {
        "deterministic_score": 60.0, "deterministic_passed": False, "deterministic_threshold": 70.0,
        "mandatory_coverage_pct": 100.0, "mandatory_skills": [], "preferred_skills": [],
    }
    candidate = _make_campaign_candidate(score_breakdown=breakdown)
    service = make_service()

    result = service._build_deterministic_score_breakdown(candidate)

    assert result.summary.experience_status is None
    assert result.summary.education_status is None
    assert result.experience_validation.required_years is None
    assert result.experience_validation.passed is None
    assert result.education_validation.required_degree is None
    assert result.education_validation.passed is None


def test_validation_status_reflects_skipped_and_data_missing_states():
    service = make_service()

    assert service._validation_status(None) is None
    assert service._validation_status({"skipped": True, "data_missing": False, "passed": True}) == "NOT_REQUIRED"
    assert service._validation_status({"skipped": False, "data_missing": True, "passed": True}) == "DATA_MISSING"
    assert service._validation_status({"skipped": False, "data_missing": False, "passed": True}) == "PASSED"
    assert service._validation_status({"skipped": False, "data_missing": False, "passed": False}) == "FAILED"
