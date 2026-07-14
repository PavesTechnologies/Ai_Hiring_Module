from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from app.exception_handler.exceptions import BadRequestError, NotFoundError
from app.models.skills import (
    JDSkillVerificationStatus,
    SkillOntology,
    UnknownSkill,
    UnknownSkillStatus,
)
from app.services.skills.skill_curation_service import SkillCurationService
from app.services.skills.skill_normalization_service import SkillMatchTier


def _service():
    repo = MagicMock()
    audit = MagicMock()
    return SkillCurationService(repo, audit), repo, audit


def _unknown_skill(raw_text="Reactjss"):
    return UnknownSkill(id=uuid4(), raw_text=raw_text, frequency=3, status=UnknownSkillStatus.PENDING)


def _skill(name="React"):
    return SkillOntology(id=uuid4(), canonical_name=name, aliases=[])


def test_map_to_existing_creates_retroactive_jd_skills_and_resolves_links():
    service, repo, audit = _service()
    unknown = _unknown_skill()
    target = _skill()
    link_a, link_b = MagicMock(jd_id=uuid4(), mandatory=True), MagicMock(jd_id=uuid4(), mandatory=False)

    repo.get_unknown_skill_by_id.return_value = unknown
    repo.get_skill_by_id.return_value = target
    repo.get_pending_jd_links.return_value = [link_a, link_b]
    repo.get_jd_skill.return_value = None  # no pre-existing JDSkill for either JD

    service.map_to_existing_skill(unknown.id, target.id, actor_id="hr-1")

    assert repo.create_jd_skill.call_count == 2
    first_call_kwargs = repo.create_jd_skill.call_args_list[0].kwargs
    assert first_call_kwargs["mandatory"] is True
    assert first_call_kwargs["match_tier"] == SkillMatchTier.MANUAL_HR.value
    assert first_call_kwargs["verification_status"] == JDSkillVerificationStatus.AUTO_VERIFIED
    assert repo.mark_jd_unknown_skill_resolved.call_count == 2
    assert repo.update_unknown_skill_status.call_args.args[1] == UnknownSkillStatus.MAPPED_TO_EXISTING
    repo.commit.assert_called_once()
    audit.log.assert_called_once()


def test_retroactive_creation_skips_duplicate_jd_skill_but_still_resolves_link():
    """A JD that independently already has a JDSkill for this canonical skill
    must not get a second insert (would violate the unique constraint), but
    its JDUnknownSkill link still needs resolving."""
    service, repo, audit = _service()
    unknown = _unknown_skill()
    target = _skill()
    link = MagicMock(jd_id=uuid4(), mandatory=True)

    repo.get_unknown_skill_by_id.return_value = unknown
    repo.get_skill_by_id.return_value = target
    repo.get_pending_jd_links.return_value = [link]
    repo.get_jd_skill.return_value = MagicMock()  # already exists

    service.map_to_existing_skill(unknown.id, target.id, actor_id="hr-1")

    repo.create_jd_skill.assert_not_called()
    repo.mark_jd_unknown_skill_resolved.assert_called_once_with(link)


def test_map_to_existing_with_save_as_alias_appends_alias():
    service, repo, audit = _service()
    unknown = _unknown_skill(raw_text="Reactjss")
    target = _skill()

    repo.get_unknown_skill_by_id.return_value = unknown
    repo.get_skill_by_id.return_value = target
    repo.get_pending_jd_links.return_value = []
    repo.find_skill_by_name_or_alias.return_value = None  # no collision

    service.map_to_existing_skill(unknown.id, target.id, actor_id="hr-1", save_as_alias=True)

    repo.append_alias.assert_called_once_with(target, "Reactjss")
    assert audit.log.call_count == 2  # UNKNOWN_SKILL_MAPPED + ALIAS_ADDED


def test_alias_duplicate_prevention_raises_when_alias_belongs_elsewhere():
    service, repo, audit = _service()
    unknown = _unknown_skill(raw_text="Reactjss")
    target = _skill(name="React")
    other_skill = _skill(name="Redux")  # a different skill already owns this alias

    repo.get_unknown_skill_by_id.return_value = unknown
    repo.get_skill_by_id.return_value = target
    repo.get_pending_jd_links.return_value = []
    repo.find_skill_by_name_or_alias.return_value = other_skill

    with pytest.raises(BadRequestError):
        service.map_to_existing_skill(unknown.id, target.id, actor_id="hr-1", save_as_alias=True)

    repo.append_alias.assert_not_called()


def test_promote_creates_new_canonical_skill_and_resolves_links():
    service, repo, audit = _service()
    unknown = _unknown_skill(raw_text="Kubernetes Operators")
    new_skill = _skill(name="Kubernetes Operators")
    link = MagicMock(jd_id=uuid4(), mandatory=True)

    repo.get_unknown_skill_by_id.return_value = unknown
    repo.find_skill_by_name_or_alias.return_value = None
    repo.create_skill_ontology.return_value = new_skill
    repo.get_pending_jd_links.return_value = [link]
    repo.get_jd_skill.return_value = None

    result = service.promote_to_canonical(unknown.id, actor_id="hr-1")

    assert result is new_skill
    repo.create_skill_ontology.assert_called_once()
    assert repo.update_unknown_skill_status.call_args.args[1] == UnknownSkillStatus.PROMOTED_TO_CANONICAL
    repo.commit.assert_called_once()


def test_promote_rejects_if_name_already_exists_in_ontology():
    service, repo, audit = _service()
    unknown = _unknown_skill(raw_text="React")
    existing = _skill(name="React")

    repo.get_unknown_skill_by_id.return_value = unknown
    repo.find_skill_by_name_or_alias.return_value = existing

    with pytest.raises(BadRequestError):
        service.promote_to_canonical(unknown.id, actor_id="hr-1")

    repo.create_skill_ontology.assert_not_called()


def test_dismiss_sets_status_and_logs_audit():
    service, repo, audit = _service()
    unknown = _unknown_skill()
    repo.get_unknown_skill_by_id.return_value = unknown

    service.dismiss(unknown.id, actor_id="hr-1")

    assert repo.update_unknown_skill_status.call_args.args[1] == UnknownSkillStatus.DISMISSED
    repo.commit.assert_called_once()
    audit.log.assert_called_once()


def test_remap_jd_skill_delegates_to_repository_and_logs_audit():
    service, repo, audit = _service()
    jd_skill = MagicMock(id=uuid4(), jd_id=uuid4(), canonical_skill_id=uuid4())
    new_skill = _skill(name="Go")

    repo.get_jd_skill_by_id.return_value = jd_skill
    repo.get_skill_by_id.return_value = new_skill

    service.remap_jd_skill(jd_skill.id, new_skill.id, actor_id="hr-1")

    repo.remap_jd_skill.assert_called_once_with(jd_skill, new_skill.id)
    repo.commit.assert_called_once()
    audit.log.assert_called_once()


def test_remap_jd_skill_raises_not_found_when_jd_skill_missing():
    service, repo, audit = _service()
    repo.get_jd_skill_by_id.return_value = None

    with pytest.raises(NotFoundError):
        service.remap_jd_skill(uuid4(), uuid4(), actor_id="hr-1")

    repo.remap_jd_skill.assert_not_called()
