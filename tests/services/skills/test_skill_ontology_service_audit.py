from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

from app.enums.constants import EntityType
from app.schemas.skill_ontology.skill_ontology_request import (
    SkillOntologyUpdateRequest,
    SkillStatusUpdateRequest,
)
from app.services.skills.SkillOntologyService import SkillOntologyService

"""
Regression coverage for the AttributeError that hit every Skill Ontology
write operation: entity_type=EntityType.SKILL doesn't exist on the
EntityType enum (only EntityType.SKILL_ONTOLOGY does, and it's the same
member skill_curation_service.py already uses for this exact model) - so
every audit_service.log() call in SkillOntologyService raised AttributeError
before ever reaching the database. These tests exercise every write path
that used to construct that call and assert it now uses SKILL_ONTOLOGY.
"""


def _make_skill(**overrides) -> SimpleNamespace:
    defaults = dict(
        id=uuid4(),
        canonical_name="Python",
        aliases=[],
        category=None,
        parent_skill_id=None,
        confidence="verified",
        source="manual entry",
        is_active=True,
        occurrence_count=0,
        created_at=datetime.now(timezone.utc),
        embedding=None,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def make_service(skill):
    repository = MagicMock()
    repository.get_skill_by_id.return_value = skill
    repository.get_parent_name.return_value = None
    repository.get_children.return_value = []
    repository.get_by_canonical_name_exact.return_value = None

    audit_service = MagicMock()
    service = SkillOntologyService(
        repository=repository,
        db=MagicMock(),
        skill_repository=MagicMock(),
        config_repository=MagicMock(),
        audit_service=audit_service,
        celery_task_log_repository=MagicMock(),
        embedding_queue_service=MagicMock(),
    )
    return service, audit_service


def test_update_skill_field_change_logs_skill_ontology_entity_type():
    skill = _make_skill(category=None)
    service, audit_service = make_service(skill)

    service.update_skill(
        skill.id,
        SkillOntologyUpdateRequest(category="Backend"),
        updated_by="user-1",
        actor_role="HR_ADMIN",
    )

    audit_service.log.assert_called()
    call_kwargs = audit_service.log.call_args_list[0].kwargs
    assert call_kwargs["entity_type"] == EntityType.SKILL_ONTOLOGY


def test_update_skill_parent_change_logs_skill_ontology_entity_type():
    new_parent = _make_skill()
    skill = _make_skill(parent_skill_id=None)
    service, audit_service = make_service(skill)
    service.repository.get_skill_by_id.side_effect = (
        lambda skill_id: new_parent if skill_id == new_parent.id else skill
    )

    service.update_skill(
        skill.id,
        SkillOntologyUpdateRequest(parent_skill_id=new_parent.id),
        updated_by="user-1",
        actor_role="HR_ADMIN",
    )

    entity_types = [call.kwargs["entity_type"] for call in audit_service.log.call_args_list]
    assert entity_types
    assert all(et == EntityType.SKILL_ONTOLOGY for et in entity_types)


def test_deactivate_skill_logs_skill_ontology_entity_type():
    skill = _make_skill(is_active=True)
    service, audit_service = make_service(skill)

    service.update_status(
        skill.id,
        SkillStatusUpdateRequest(is_active=False),
        updated_by="user-1",
        actor_role="HR_ADMIN",
    )

    audit_service.log.assert_called_once()
    assert audit_service.log.call_args.kwargs["entity_type"] == EntityType.SKILL_ONTOLOGY


def test_reactivate_skill_logs_skill_ontology_entity_type():
    skill = _make_skill(is_active=False)
    service, audit_service = make_service(skill)

    service.update_status(
        skill.id,
        SkillStatusUpdateRequest(is_active=True),
        updated_by="user-1",
        actor_role="HR_ADMIN",
    )

    audit_service.log.assert_called_once()
    assert audit_service.log.call_args.kwargs["entity_type"] == EntityType.SKILL_ONTOLOGY


def test_update_skill_alias_merge_logs_skill_ontology_entity_type():
    skill = _make_skill(aliases=["py"])
    service, audit_service = make_service(skill)
    service.repository.find_skill_by_alias.return_value = None

    service.update_skill(
        skill.id,
        SkillOntologyUpdateRequest(aliases=["python3"]),
        updated_by="user-1",
        actor_role="HR_ADMIN",
    )

    audit_service.log.assert_called()
    assert audit_service.log.call_args_list[0].kwargs["entity_type"] == EntityType.SKILL_ONTOLOGY
