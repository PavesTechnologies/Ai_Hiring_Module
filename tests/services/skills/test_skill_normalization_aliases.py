from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

from app.schemas.ai.jd_extraction_response import JDSkillSpec
from app.services.skills.skill_normalization_service import SkillMatchTier, SkillNormalizationService

SPM_ID, ITBM_ID, PPM_ID, JAVA_ID = uuid4(), uuid4(), uuid4(), uuid4()


def _service(catalog):
    skill_repository = MagicMock()
    skill_repository.list_active_skills.return_value = catalog
    skill_repository.find_by_embedding.return_value = None
    return SkillNormalizationService(skill_repository, MagicMock(), cache_service=None)


def _skill(skill_id, name, aliases=None):
    return SimpleNamespace(id=skill_id, canonical_name=name, aliases=aliases or [])


def test_name_match_keeps_resolved_aliases_as_alias_skill_ids():
    service = _service([
        _skill(SPM_ID, "ServiceNow SPM"), _skill(ITBM_ID, "ITBM"), _skill(PPM_ID, "Project Portfolio Management", ["PPM"]),
    ])

    [result] = service.normalize_skills([JDSkillSpec("ServiceNow SPM", "core", ("ITBM", "PPM"))], [])

    assert result.canonical_skill_id == SPM_ID
    assert result.match_tier == SkillMatchTier.EXACT
    assert result.importance == "core"
    assert result.alias_skill_ids == [ITBM_ID, PPM_ID]


def test_unknown_name_falls_back_to_first_resolving_alias():
    service = _service([_skill(ITBM_ID, "ITBM"), _skill(PPM_ID, "PPM")])

    [result] = service.normalize_skills([JDSkillSpec("ServiceNow SPM", "core", ("Unmapped", "ITBM", "PPM"))], [])

    assert result.canonical_skill_id == ITBM_ID
    assert result.raw_text == "ServiceNow SPM"
    assert result.mandatory is True
    assert result.importance == "core"
    assert result.alias_skill_ids == [PPM_ID]


def test_alias_resolving_to_the_same_skill_is_not_duplicated():
    service = _service([_skill(SPM_ID, "ServiceNow SPM", ["ITBM"])])

    [result] = service.normalize_skills([JDSkillSpec("ServiceNow SPM", "core", ("ITBM",))], [])

    assert result.canonical_skill_id == SPM_ID
    assert result.alias_skill_ids == []


def test_unresolved_aliases_never_produce_extra_results():
    service = _service([_skill(JAVA_ID, "Java")])

    results = service.normalize_skills([JDSkillSpec("Java", "core", ("Nope",))], [JDSkillSpec("Docker", None, ())])

    assert len(results) == 2
    assert results[0].alias_skill_ids == []
    assert results[1].canonical_skill_id is None
    assert results[1].mandatory is False


def test_plain_string_resume_skills_still_supported():
    service = _service([_skill(JAVA_ID, "Java")])

    [result] = service.normalize_skills(["Java"], [])

    assert result.canonical_skill_id == JAVA_ID
    assert result.importance is None
    assert result.alias_skill_ids == []
