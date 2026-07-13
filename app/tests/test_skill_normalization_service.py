from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from app.models.skills import SkillOntology
from app.repositories.skill_repository import SEMANTIC_SIMILARITY_THRESHOLD
from app.services.skills.skill_normalization_service import (
    SkillMatchTier,
    SkillNormalizationService,
    verification_status_for_tier,
)
from app.models.skills import JDSkillVerificationStatus


def _skill(canonical_name, aliases=None):
    return SkillOntology(
        id=uuid4(),
        canonical_name=canonical_name,
        aliases=aliases or [],
        is_active=True,
    )


def _service(catalog, embedding_result=None):
    repo = MagicMock()
    repo.list_active_skills.return_value = catalog
    repo.find_by_embedding.return_value = embedding_result
    embedding_service = MagicMock()
    embedding_service.generate_embedding.return_value = [0.1] * 384
    return SkillNormalizationService(repo, embedding_service), repo, embedding_service


def test_exact_canonical_match():
    python_skill = _skill("Python")
    service, repo, _ = _service([python_skill])

    results = service.normalize_skills(["Python"], [])

    assert results[0].canonical_skill_id == python_skill.id
    assert results[0].match_tier == SkillMatchTier.EXACT
    assert results[0].confidence == 1.0


def test_exact_alias_match():
    react_skill = _skill("React", aliases=["ReactJS"])
    service, repo, _ = _service([react_skill])

    results = service.normalize_skills(["ReactJS"], [])

    assert results[0].canonical_skill_id == react_skill.id
    assert results[0].match_tier == SkillMatchTier.ALIAS


def test_case_insensitive_canonical_match():
    python_skill = _skill("Python")
    service, repo, _ = _service([python_skill])

    results = service.normalize_skills(["python"], [])

    assert results[0].canonical_skill_id == python_skill.id
    assert results[0].match_tier == SkillMatchTier.CASE_INSENSITIVE


def test_case_insensitive_alias_match():
    react_skill = _skill("React", aliases=["ReactJS"])
    service, repo, _ = _service([react_skill])

    results = service.normalize_skills(["reactjs"], [])

    assert results[0].canonical_skill_id == react_skill.id
    assert results[0].match_tier == SkillMatchTier.CASE_INSENSITIVE


def test_rule_based_canonical_match():
    node_skill = _skill("Node.js")
    service, repo, _ = _service([node_skill])

    results = service.normalize_skills(["Node JS"], [])

    assert results[0].canonical_skill_id == node_skill.id
    assert results[0].match_tier == SkillMatchTier.RULE_BASED


def test_rule_based_alias_match():
    node_skill = _skill("Node", aliases=["Node.js"])
    service, repo, _ = _service([node_skill])

    results = service.normalize_skills(["node js"], [])

    assert results[0].canonical_skill_id == node_skill.id
    assert results[0].match_tier == SkillMatchTier.RULE_BASED


def test_fuzzy_canonical_match():
    python_skill = _skill("Python")
    service, repo, _ = _service([python_skill])

    results = service.normalize_skills(["Pythonn"], [])  # one-letter typo, ratio 92.3 (>= 85 threshold)

    assert results[0].canonical_skill_id == python_skill.id
    assert results[0].match_tier == SkillMatchTier.FUZZY


def test_fuzzy_does_not_search_aliases():
    """
    Finalized pipeline: fuzzy is canonical-only. A typo of an ALIAS must not
    fuzzy-match — it should fall through toward semantic/unknown instead.
    """
    skill = _skill("Kubernetes", aliases=["K8s"])
    service, repo, _ = _service([skill], embedding_result=None)

    results = service.normalize_skills(["K8x"], [])  # typo of the alias, not the canonical name

    assert results[0].match_tier != SkillMatchTier.FUZZY


def test_semantic_canonical_match():
    skill = _skill("Machine Learning")
    service, repo, _ = _service([skill], embedding_result=(skill, 0.90))

    results = service.normalize_skills(["ML systems design"], [])

    assert results[0].canonical_skill_id == skill.id
    assert results[0].match_tier == SkillMatchTier.SEMANTIC
    assert results[0].confidence == 0.90


def test_semantic_below_threshold_falls_to_unknown():
    skill = _skill("Machine Learning")
    below_threshold = SEMANTIC_SIMILARITY_THRESHOLD - 0.05
    service, repo, _ = _service([skill], embedding_result=(skill, below_threshold))

    results = service.normalize_skills(["completely unrelated text"], [])

    assert results[0].canonical_skill_id is None
    assert results[0].match_tier == SkillMatchTier.UNKNOWN


def test_unknown_when_nothing_matches():
    service, repo, _ = _service([], embedding_result=None)

    results = service.normalize_skills(["Some Totally Novel Skill"], [])

    assert results[0].canonical_skill_id is None
    assert results[0].match_tier == SkillMatchTier.UNKNOWN
    assert results[0].confidence is None


def test_short_circuit_stops_before_semantic_on_exact_match():
    python_skill = _skill("Python")
    service, repo, embedding_service = _service([python_skill])

    service.normalize_skills(["Python"], [])

    embedding_service.generate_embedding.assert_not_called()
    repo.find_by_embedding.assert_not_called()


def test_normalize_collapses_whitespace_and_unicode_without_lowercasing():
    cleaned = SkillNormalizationService._normalize("  React  Native  ")
    assert cleaned == "React Native"


def test_mandatory_and_preferred_are_both_tagged_correctly():
    python_skill = _skill("Python")
    service, repo, _ = _service([python_skill])

    results = service.normalize_skills(["Python"], ["Python"])

    assert results[0].mandatory is True
    assert results[1].mandatory is False


@pytest.mark.parametrize(
    "tier,expected",
    [
        (SkillMatchTier.EXACT, JDSkillVerificationStatus.AUTO_VERIFIED),
        (SkillMatchTier.ALIAS, JDSkillVerificationStatus.AUTO_VERIFIED),
        (SkillMatchTier.CASE_INSENSITIVE, JDSkillVerificationStatus.AUTO_VERIFIED),
        (SkillMatchTier.RULE_BASED, JDSkillVerificationStatus.AUTO_VERIFIED),
        (SkillMatchTier.FUZZY, JDSkillVerificationStatus.PENDING_REVIEW),
        (SkillMatchTier.SEMANTIC, JDSkillVerificationStatus.PENDING_REVIEW),
        # FIX 7: a human decision is at least as trustworthy as a
        # deterministic string match — must not be classified PENDING_REVIEW.
        (SkillMatchTier.MANUAL_HR, JDSkillVerificationStatus.AUTO_VERIFIED),
    ],
)
def test_verification_status_for_tier(tier, expected):
    assert verification_status_for_tier(tier) == expected
