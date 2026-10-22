"""Phase 3 Step 6: a skill's short description (used by paraphrase search) is saved, trimmed and audited."""
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from app.schemas.skill_ontology.skill_ontology_request import SkillOntologyUpdateRequest
from app.services.skills.SkillOntologyService import SkillOntologyService


def _service(skill):
    repository = MagicMock()
    repository.get_skill_by_id.return_value = skill
    repository.get_parent_name.return_value = None
    repository.get_children.return_value = []
    repository.get_by_canonical_name_exact.return_value = None
    audit = MagicMock()
    return SkillOntologyService(repository=repository, db=MagicMock(), skill_repository=MagicMock(),
                                config_repository=MagicMock(), audit_service=audit,
                                celery_task_log_repository=MagicMock(), embedding_queue_service=MagicMock()), audit


def _skill(description=None):
    return SimpleNamespace(id=uuid4(), canonical_name="Service Catalog", aliases=[], category=None,
                           description=description, parent_skill_id=None, confidence="verified",
                           source="manual entry", is_active=True, occurrence_count=0,
                           created_at=datetime.now(timezone.utc), embedding=None)


def test_a_description_is_saved_trimmed_and_audited():
    skill = _skill()
    service, audit = _service(skill)
    response = service.update_skill(skill.id, SkillOntologyUpdateRequest(description="  ServiceNow catalogue items  "),
                                    updated_by="hr-1", actor_role="HR_ADMIN")
    assert skill.description == "ServiceNow catalogue items"
    details = audit.log.call_args_list[0].kwargs["details"]
    assert "ServiceNow catalogue items" in str(details) and response.description == "ServiceNow catalogue items"


def test_a_blank_description_clears_it():
    skill = _skill("old")
    service, _ = _service(skill)
    service.update_skill(skill.id, SkillOntologyUpdateRequest(description="   "), updated_by="hr-1",
                         actor_role="HR_ADMIN")
    assert skill.description is None


def test_a_description_is_at_most_500_characters():
    with pytest.raises(ValueError):
        SkillOntologyUpdateRequest(description="x" * 501)


def test_a_not_this_skill_text_is_saved_trimmed_and_audited():
    skill = _skill(description="ServiceNow catalogue items")
    service, audit = _service(skill)
    response = service.update_skill(
        skill.id, SkillOntologyUpdateRequest(not_description="  not a customer-facing product catalogue  "),
        updated_by="hr-1", actor_role="HR_ADMIN")
    assert skill.not_description == "not a customer-facing product catalogue"
    assert skill.description == "ServiceNow catalogue items"                  # the description is untouched
    details = str(audit.log.call_args_list[0].kwargs["details"])
    assert "not_description" in details and response.not_description == "not a customer-facing product catalogue"

