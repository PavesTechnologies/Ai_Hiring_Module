"""One alias, one active skill: the skill service refuses an alias another active skill already has, naming it."""
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.exception_handler.exceptions import ConflictError
from app.schemas.skill_ontology.skill_ontology_request import SkillCreateRequest, SkillOntologyUpdateRequest
from tests.db.test_campaign_candidate_delete_scored import db  # noqa: F401 - fixture (real Postgres, rolled back)
from tests.services.skills.test_skill_ontology_service_audit import _make_skill, make_service


def _owner(name="Docker Compose"):
    return SimpleNamespace(id=uuid4(), canonical_name=name, is_active=True)


def test_adding_an_alias_another_active_skill_has_is_refused_naming_it():
    skill = _make_skill(canonical_name="Jetpack Compose", aliases=[])
    service, audit = make_service(skill)
    service.repository.find_active_skill_by_alias.return_value = _owner("Docker Compose")
    with pytest.raises(ConflictError) as exc:
        service.update_skill(skill.id, SkillOntologyUpdateRequest(aliases=["compose"]), updated_by="u1",
                             actor_role="HR_ADMIN")
    assert "'compose'" in str(exc.value) and "Docker Compose" in str(exc.value)
    service.repository.find_active_skill_by_alias.assert_called_with("compose", exclude_id=skill.id)
    assert skill.aliases == []
    audit.log.assert_not_called()


def test_an_alias_no_other_active_skill_has_is_added():
    skill = _make_skill(aliases=["py"])
    service, _ = make_service(skill)
    service.repository.find_active_skill_by_alias.return_value = None
    service.update_skill(skill.id, SkillOntologyUpdateRequest(aliases=["python3"]), updated_by="u1",
                         actor_role="HR_ADMIN")
    assert skill.aliases == ["py", "python3"]


def test_an_alias_the_skill_already_has_is_not_checked_again():
    skill = _make_skill(aliases=["py"])
    service, _ = make_service(skill)
    service.update_skill(skill.id, SkillOntologyUpdateRequest(aliases=["py"]), updated_by="u1", actor_role="HR_ADMIN")
    service.repository.find_active_skill_by_alias.assert_not_called()


def test_creating_a_skill_with_an_alias_another_active_skill_has_is_refused():
    service, _ = make_service(_make_skill())
    service.repository.find_active_skill_by_alias.side_effect = (
        lambda alias, exclude_id=None: _owner("AWS SNS") if alias == "Simple Notification Service" else None)
    with pytest.raises(ConflictError) as exc:
        service.create_skill(SkillCreateRequest(canonical_name="SNS", aliases=["Notify", "Simple Notification Service"]))
    assert "AWS SNS" in str(exc.value)
    service.repository.create_skill.assert_not_called()


def _row(n, name, aliases):
    return {"row_number": n, "canonical_name": name, "aliases": aliases, "confidence": "verified", "parent_skill": None,
            "category": None, "source": "manual entry", "is_active": True}


def test_bulk_import_validation_flags_aliases_of_active_skills_and_of_other_rows():
    service, _ = make_service(_make_skill())
    service.repository.get_all_canonical_names.return_value = {}
    service.repository.find_active_skill_by_alias.side_effect = (
        lambda alias, exclude_id=None: _owner("ScalaTest") if alias.lower() == "testing" else None)
    result = service._validate_rows([
        _row(2, "Kotest", ["Testing"]),                      # another active skill has it
        _row(3, "ExUnit", ["Elixir tests", "Unit Tests"]),
        _row(4, "Molecule", ["unit tests"]),                 # an earlier row of this file has it (any case)
        _row(5, "Rocket", ["Rocket.rs"]),
    ])
    messages = {e.row: e.message for e in result.validation_errors if e.column == "aliases"}
    assert messages == {2: "Alias 'Testing' already belongs to the active skill 'ScalaTest'.",
                        4: "Alias 'unit tests' is also given to 'ExUnit' in this file."}


def test_the_lookup_is_case_insensitive_ignores_inactive_skills_and_the_skill_itself(db):  # noqa: F811
    """Against real Postgres (rolled back)."""
    from app.models.skills import SkillOntology
    from app.repositories.skill_ontology_repository import SkillOntologyRepository
    tag = uuid4().hex[:8]
    active = SkillOntology(id=uuid4(), canonical_name=f"Alias test active {tag}", aliases=[f"Shared {tag}"],
                           is_active=True, confidence="verified", source="manual entry")
    inactive = SkillOntology(id=uuid4(), canonical_name=f"Alias test inactive {tag}", aliases=[f"Old {tag}"],
                             is_active=False, confidence="verified", source="manual entry")
    db.add_all([active, inactive])
    db.flush()
    repo = SkillOntologyRepository(db)
    assert repo.find_active_skill_by_alias(f"SHARED {tag}").id == active.id
    assert repo.find_active_skill_by_alias(f"shared {tag}", exclude_id=active.id) is None
    assert repo.find_active_skill_by_alias(f"Old {tag}") is None              # a merged-away duplicate never blocks
