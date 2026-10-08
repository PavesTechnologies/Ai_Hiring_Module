from app.models.skills import JDSkillImportance
from app.services.jd.jd_skill_importance import importance_from_extraction

"""
HR-approved (formerly unknown) JD skills must keep the core/supporting
class JD extraction gave them, or the JD silently loses its core-skill gate.
"""

EXTRACTED = {
    "required_skills": {
        "core": ["AI Transformation", "Program Management"],
        "supporting": ["Risk Management", "Executive Reporting"],
    },
    "aliases": {"Program Management": ["PgM"]},
}


def test_core_and_supporting_are_read_back_by_name():
    assert importance_from_extraction(EXTRACTED, "AI Transformation") == JDSkillImportance.CORE
    assert importance_from_extraction(EXTRACTED, "risk management") == JDSkillImportance.SUPPORTING


def test_matches_through_aliases_and_normalized_keys():
    assert importance_from_extraction(EXTRACTED, "PgM") == JDSkillImportance.CORE
    assert importance_from_extraction(EXTRACTED, None, "executive-reporting") == JDSkillImportance.SUPPORTING


def test_unlisted_or_missing_data_is_never_guessed():
    assert importance_from_extraction(EXTRACTED, "Kubernetes") is None
    assert importance_from_extraction(None, "Program Management") is None
    assert importance_from_extraction({"required_skills": None}, "Program Management") is None
    assert importance_from_extraction(EXTRACTED) is None
