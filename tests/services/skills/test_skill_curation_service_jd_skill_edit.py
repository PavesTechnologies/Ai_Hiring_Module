from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.enums.constants import ActionType
from app.models.skills import JDSkillImportance
from app.services.skills.skill_curation_service import SkillCurationService


def _jd_skill(mandatory=True, importance=JDSkillImportance.SUPPORTING):
    return SimpleNamespace(
        id=uuid4(), jd_id=uuid4(), canonical_skill_id=uuid4(),
        mandatory=mandatory, importance=importance, match_tier="EXACT",
    )


def _service(jd_skill, active_campaign=False, core_count=2):
    skill_repository = MagicMock()
    skill_repository.get_jd_skill_by_id.return_value = jd_skill
    skill_repository.count_core_jd_skills.return_value = core_count

    def _update(skill, mandatory, importance):
        skill.mandatory, skill.importance, skill.match_tier = mandatory, importance, "MANUAL_HR"
        return skill
    skill_repository.update_jd_skill_classification.side_effect = _update

    jd_repository = MagicMock()
    jd_repository.has_active_campaign.return_value = active_campaign
    audit_service = MagicMock()
    embedding_queue_service = MagicMock()

    service = SkillCurationService(
        skill_repository=skill_repository,
        audit_service=audit_service,
        embedding_queue_service=embedding_queue_service,
        encryption_service=MagicMock(),
        resume_repository=MagicMock(),
        reevaluation_queue_service=MagicMock(),
        jd_repository=jd_repository,
    )
    return service, skill_repository, audit_service, embedding_queue_service


def _status(exc_info) -> int:
    return exc_info.value.status_code


# ---------------------------------------------------------------- update


def test_promote_preferred_skill_to_mandatory_core():
    jd_skill = _jd_skill(mandatory=False, importance=None)
    service, skill_repository, audit_service, embedding_queue_service = _service(jd_skill)

    result = service.update_jd_skill(jd_skill.id, mandatory=True, importance="core", actor_id="hr-1")

    skill_repository.update_jd_skill_classification.assert_called_once_with(jd_skill, True, JDSkillImportance.CORE)
    assert result.match_tier == "MANUAL_HR"
    skill_repository.commit.assert_called_once()
    audit_kwargs = audit_service.log.call_args.kwargs
    assert audit_kwargs["action_type"] == ActionType.JD_SKILL_UPDATED
    assert audit_kwargs["details"]["previous"] == {"mandatory": False, "importance": None}
    assert audit_kwargs["details"]["new"] == {"mandatory": True, "importance": "CORE"}
    embedding_queue_service.queue_jd_embedding.assert_called_once_with(jd_skill.jd_id, force_regenerate=True)


def test_demote_to_preferred_clears_importance():
    jd_skill = _jd_skill(mandatory=True, importance=JDSkillImportance.SUPPORTING)
    service, skill_repository, _, _ = _service(jd_skill)

    service.update_jd_skill(jd_skill.id, mandatory=False, importance="core", actor_id="hr-1")

    skill_repository.update_jd_skill_classification.assert_called_once_with(jd_skill, False, None)


def test_mandatory_without_importance_is_rejected():
    jd_skill = _jd_skill(mandatory=False, importance=None)
    service, skill_repository, _, _ = _service(jd_skill)

    with pytest.raises(HTTPException) as exc_info:
        service.update_jd_skill(jd_skill.id, mandatory=True, importance=None, actor_id="hr-1")

    assert _status(exc_info) == 400
    skill_repository.update_jd_skill_classification.assert_not_called()


def test_downgrading_last_core_skill_is_rejected():
    jd_skill = _jd_skill(importance=JDSkillImportance.CORE)
    service, skill_repository, _, _ = _service(jd_skill, core_count=1)

    with pytest.raises(HTTPException) as exc_info:
        service.update_jd_skill(jd_skill.id, mandatory=True, importance="supporting", actor_id="hr-1")

    assert _status(exc_info) == 400
    skill_repository.update_jd_skill_classification.assert_not_called()


def test_downgrading_core_skill_allowed_when_another_core_remains():
    jd_skill = _jd_skill(importance=JDSkillImportance.CORE)
    service, skill_repository, _, _ = _service(jd_skill, core_count=2)

    service.update_jd_skill(jd_skill.id, mandatory=True, importance="supporting", actor_id="hr-1")

    skill_repository.update_jd_skill_classification.assert_called_once_with(
        jd_skill, True, JDSkillImportance.SUPPORTING,
    )


def test_update_blocked_while_jd_has_active_campaign():
    jd_skill = _jd_skill()
    service, skill_repository, _, _ = _service(jd_skill, active_campaign=True)

    with pytest.raises(HTTPException) as exc_info:
        service.update_jd_skill(jd_skill.id, mandatory=True, importance="core", actor_id="hr-1")

    assert _status(exc_info) == 409
    skill_repository.update_jd_skill_classification.assert_not_called()


def test_update_unknown_jd_skill_is_404():
    service, skill_repository, _, _ = _service(None)

    with pytest.raises(HTTPException) as exc_info:
        service.update_jd_skill(uuid4(), mandatory=True, importance="core", actor_id="hr-1")

    assert _status(exc_info) == 404


# ---------------------------------------------------------------- remap


def test_remap_blocked_while_jd_has_active_campaign():
    jd_skill = _jd_skill()
    service, skill_repository, _, _ = _service(jd_skill, active_campaign=True)

    with pytest.raises(HTTPException) as exc_info:
        service.remap_jd_skill(jd_skill.id, uuid4(), actor_id="hr-1")

    assert _status(exc_info) == 409
    skill_repository.remap_jd_skill.assert_not_called()


# ---------------------------------------------------------------- remove


def test_remove_deletes_only_the_jd_link_and_audits():
    jd_skill = _jd_skill(importance=JDSkillImportance.SUPPORTING)
    service, skill_repository, audit_service, embedding_queue_service = _service(jd_skill)

    service.remove_jd_skill(jd_skill.id, actor_id="hr-1")

    skill_repository.delete_jd_skill.assert_called_once_with(jd_skill)
    skill_repository.commit.assert_called_once()
    audit_kwargs = audit_service.log.call_args.kwargs
    assert audit_kwargs["action_type"] == ActionType.JD_SKILL_REMOVED
    assert audit_kwargs["details"]["canonical_skill_id"] == str(jd_skill.canonical_skill_id)
    embedding_queue_service.queue_jd_embedding.assert_called_once_with(jd_skill.jd_id, force_regenerate=True)


def test_removing_last_core_skill_is_rejected():
    jd_skill = _jd_skill(importance=JDSkillImportance.CORE)
    service, skill_repository, _, _ = _service(jd_skill, core_count=1)

    with pytest.raises(HTTPException) as exc_info:
        service.remove_jd_skill(jd_skill.id, actor_id="hr-1")

    assert _status(exc_info) == 400
    skill_repository.delete_jd_skill.assert_not_called()


def test_remove_blocked_while_jd_has_active_campaign():
    jd_skill = _jd_skill()
    service, skill_repository, _, _ = _service(jd_skill, active_campaign=True)

    with pytest.raises(HTTPException) as exc_info:
        service.remove_jd_skill(jd_skill.id, actor_id="hr-1")

    assert _status(exc_info) == 409
    skill_repository.delete_jd_skill.assert_not_called()


def test_removing_preferred_skill_never_checks_core_count():
    jd_skill = _jd_skill(mandatory=False, importance=None)
    service, skill_repository, _, _ = _service(jd_skill, core_count=0)

    service.remove_jd_skill(jd_skill.id, actor_id="hr-1")

    skill_repository.count_core_jd_skills.assert_not_called()
    skill_repository.delete_jd_skill.assert_called_once_with(jd_skill)
