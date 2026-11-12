"""Settings phase 3: every reference-data edit stamps the affected candidates out of date (agreed 2026-11-12)."""
from types import SimpleNamespace as NS
from unittest.mock import MagicMock, patch
from uuid import uuid4

from app.services.skills.certification_mapping_service import CertificationMappingService

MARK = "app.services.settings.reference_impact.mark_after_edit"


def _certification_service(skills=()):
    repository, skill_repository, audit = MagicMock(), MagicMock(), MagicMock()
    certification = NS(id=uuid4(), name="CKA", name_variants=[], skills=list(skills))
    repository.get.return_value = certification
    skill_repository.get_skill_by_id.return_value = NS(canonical_name="Kubernetes", is_active=True)
    service = CertificationMappingService(repository, skill_repository, lambda: [], audit)
    return service, certification, audit


def test_mapping_a_skill_to_a_certification_marks_campaigns_using_that_skill():
    skill_id = uuid4()
    service, certification, audit = _certification_service()
    with patch(MARK, return_value=3) as mark:
        service.add_skill(certification.id, canonical_skill_id=skill_id, sense_key=None, actor_id="hr",
                          actor_role="HR_ADMIN")
    assert mark.call_args.kwargs["skill_ids"] == [skill_id]
    assert audit.log.call_args.kwargs["details"]["candidates_marked_out_of_date"] == 3


def test_unmapping_a_skill_and_removing_a_certification_mark_too():
    skill_id = uuid4()
    service, certification, audit = _certification_service([NS(canonical_skill_id=skill_id, sense_key=None)])
    with patch(MARK, return_value=1) as mark:
        service.remove_skill(certification.id, skill_id, actor_id="hr", actor_role="HR_ADMIN")
    assert mark.call_args.kwargs["skill_ids"] == [skill_id]
    service, certification, audit = _certification_service([NS(canonical_skill_id=skill_id, sense_key=None)])
    with patch(MARK, return_value=2) as mark:
        service.remove_certification(certification.id, actor_id="hr", actor_role="HR_ADMIN")
    assert mark.call_args.kwargs["skill_ids"] == [str(skill_id)]          # read before the mappings go
    assert audit.log.call_args.kwargs["details"]["candidates_marked_out_of_date"] == 2


def test_a_catalogue_skill_edit_marks_campaigns_using_it():
    from app.services.skills.SkillOntologyService import SkillOntologyService
    service = SkillOntologyService.__new__(SkillOntologyService)
    service.db = MagicMock()
    skill_id = uuid4()
    with patch(MARK, return_value=5) as mark:
        assert service._mark_out_of_date([skill_id], "The catalogue skill 'Python'") == 5
    assert mark.call_args.args[1] == "skill" and mark.call_args.kwargs["skill_ids"] == [skill_id]


def test_every_skill_edit_path_calls_the_marking():
    """update_skill, update_status and the bulk import (_execute_bulk_import) each stamp before they commit."""
    import inspect
    from app.services.skills.SkillOntologyService import SkillOntologyService
    for name in ("update_skill", "update_status", "_execute_bulk_import"):
        assert "_mark_out_of_date(" in inspect.getsource(getattr(SkillOntologyService, name)), name
