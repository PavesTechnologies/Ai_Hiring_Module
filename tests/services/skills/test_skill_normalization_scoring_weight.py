import pytest

from app.services.skills.skill_normalization_service import SkillMatchTier, scoring_weight_for_tier


@pytest.mark.parametrize(
    "tier,expected_weight",
    [
        (SkillMatchTier.ALIAS, 1.0),
        (SkillMatchTier.EXACT, 1.0),
        (SkillMatchTier.CASE_INSENSITIVE, 1.0),
        (SkillMatchTier.RULE_BASED, 1.0),
        (SkillMatchTier.MANUAL_HR, 1.0),
        (SkillMatchTier.FUZZY, 0.8),
        (SkillMatchTier.SEMANTIC, 0.8),
        (SkillMatchTier.UNKNOWN, 0.0),
    ],
)
def test_scoring_weight_for_tier_matches_m07_e01_s03_t02(tier, expected_weight):
    """
    M07-E01 S03-T02: candidate_scoring_weight by match tier - deterministic
    string-comparison tiers (alias/high-confidence) are trusted at full
    weight, FUZZY/SEMANTIC (partial-fuzzy/vector) count for less, UNKNOWN
    (never resolved to a canonical skill) contributes nothing.
    """
    assert scoring_weight_for_tier(tier) == expected_weight
