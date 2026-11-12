"""
Phase 3 Step 2: related education fields per role domain.

On a JD whose education is mandatory and whose role domain is set in
calibration, a field in the same family (education_field_relations) counts as
a related match and passes. Families are scoped by domain: CS <-> ECE is one
family for SOFTWARE JDs, not for others. Computer Applications (BCA / MCA) is
recognised from the field text or the degree.
"""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from app.enums.education import EducationField
from app.exception_handler.exceptions import ConflictError, NotFoundError, UnprocessableError
from app.services.campaign.experience_education_validation_service import ExperienceEducationValidationService
from app.services.education.education_field_family_service import (
    EducationFieldFamilyService, field_families_for_jd, ordered_pair,
)
from app.services.education.education_matching_service import classify_field, classify_field_from_degree

SERVICE = "app.services.education.education_field_family_service"
SOFTWARE = frozenset({frozenset({"COMPUTER_SCIENCE", "INFORMATION_TECHNOLOGY"}),
                      frozenset({"COMPUTER_SCIENCE", "ELECTRONICS_ENGINEERING"}),
                      frozenset({"COMPUTER_APPLICATIONS", "COMPUTER_SCIENCE"})})
CS_BACHELOR = {"degree": "B.E. / B.Tech", "degree_level": "BACHELOR", "field": "Computer Science",
               "field_normalized": "COMPUTER_SCIENCE"}


def _entry(field, field_normalized, degree="B.Tech"):
    return {"degree": degree, "degree_level": "BACHELOR", "field": field, "field_normalized": field_normalized}


def _education(entries, families=frozenset()):
    return ExperienceEducationValidationService().validate_education(
        None, entries, 0.0, jd_extracted_education=CS_BACHELOR, field_families=families)


# --- the gate -----------------------------------------------------------------

def test_phase3_f3_information_technology_passes_a_software_cs_requirement():
    f3 = [_entry("Information Technology", "INFORMATION_TECHNOLOGY")]
    assert _education(f3)["passed"] is False                        # before: a mismatch
    result = _education(f3, SOFTWARE)
    assert result["passed"] is True and result["field_match"] == "RELATED_FIELD_MATCH"


def test_phase3_f4_electronics_passes_a_software_cs_requirement():
    f4 = [_entry("Electronics and Communication Engineering", "ELECTRONICS_ENGINEERING", "B.E.")]
    assert _education(f4)["passed"] is False
    assert _education(f4, SOFTWARE)["passed"] is True


def test_an_unrelated_field_still_fails():
    f6 = [_entry("Mechanical Engineering", "MECHANICAL_ENGINEERING")]
    assert _education(f6, SOFTWARE)["passed"] is False


@pytest.mark.parametrize("entry", [
    _entry("Computer Applications", "OTHER", "BCA"),           # extraction has no such field: OTHER
    _entry(None, "UNKNOWN", "MCA"),                              # only the degree says it
    _entry("Computer Applications", "UNKNOWN", "Bachelor of Computer Applications"),
])
def test_computer_applications_is_recognised_and_related_on_software_jds(entry):
    assert _education([entry], SOFTWARE)["passed"] is True
    assert _education([entry])["passed"] is False


def test_classifying_computer_applications():
    assert classify_field("Computer Applications") == EducationField.COMPUTER_APPLICATIONS
    assert classify_field_from_degree("BCA") == EducationField.COMPUTER_APPLICATIONS
    assert classify_field_from_degree("M.C.A.") == EducationField.COMPUTER_APPLICATIONS
    assert classify_field_from_degree("B.Tech") == EducationField.UNKNOWN
    assert classify_field_from_degree("Abcam diploma") == EducationField.UNKNOWN   # not a word match


def test_a_known_other_field_is_still_other():
    result = _education([_entry("Fine Arts", "OTHER", "BFA")], SOFTWARE)
    assert result["passed"] is False and result["candidate_field"] == "OTHER"


# --- which families apply -------------------------------------------------------

def _jd(domain="SOFTWARE", education="MANDATORY"):
    return SimpleNamespace(role_domain=domain, education_requirement=SimpleNamespace(value=education))


def _rows(domain):
    pairs = {"SOFTWARE": [("COMPUTER_SCIENCE", "ELECTRONICS_ENGINEERING")],
             "DATA": [("MATHEMATICS", "STATISTICS")]}[domain]
    return [SimpleNamespace(field_a=a, field_b=b) for a, b in pairs]


@pytest.fixture
def repo():
    with patch(f"{SERVICE}.EducationFieldRelationRepository") as cls:
        cls.return_value.for_domain.side_effect = _rows
        yield cls.return_value


def test_families_are_scoped_to_the_jds_role_domain(repo):
    assert field_families_for_jd(MagicMock(), _jd("SOFTWARE")) == {
        frozenset({"COMPUTER_SCIENCE", "ELECTRONICS_ENGINEERING"})}
    assert field_families_for_jd(MagicMock(), _jd("DATA")) == {frozenset({"MATHEMATICS", "STATISTICS"})}


def test_no_families_without_a_role_domain_or_when_education_is_preferred(repo):
    assert field_families_for_jd(MagicMock(), _jd(None)) == frozenset()
    assert field_families_for_jd(MagicMock(), _jd("SOFTWARE", "PREFERRED")) == frozenset()
    assert field_families_for_jd(MagicMock(), _jd("NOT_A_DOMAIN")) == frozenset()
    repo.for_domain.assert_not_called()


def test_the_scoring_inputs_pass_the_jds_families():
    from app.services.campaign import validation_inputs as tasks
    config = MagicMock()
    config.get_configs_by_keys.return_value = {}
    jd = SimpleNamespace(min_experience_years=None, extracted_json={"education": CS_BACHELOR}, education_criteria={},
                         experience_tolerance_years=None, calibrated_min_experience_years=None,
                         internship_credit_pct=None, role_domain="SOFTWARE",
                         calibration_status=SimpleNamespace(value="CALIBRATED"),
                         education_requirement=SimpleNamespace(value="MANDATORY"))
    resume = SimpleNamespace(parsed_json={"work_experience": [], "education": [
        _entry("Information Technology", "INFORMATION_TECHNOLOGY")]})
    with patch(f"{tasks.__name__}.field_families_for_jd", return_value=SOFTWARE) as families:
        _, education, *_ = tasks.build_validation_inputs(config, jd, resume)
    families.assert_called_once_with(config.db, jd)
    assert education["passed"] is True and education["field_match"] == "RELATED_FIELD_MATCH"


# --- editing (HR_ADMIN) ---------------------------------------------------------

def _service(existing=None):
    repository, audit = MagicMock(), MagicMock()
    repository.find.return_value = existing
    repository.add.side_effect = lambda r: (setattr(r, "id", uuid4()), r)[1]
    return EducationFieldFamilyService(repository, audit), repository, audit


def test_a_pair_is_stored_in_order_and_audited():
    service, repository, audit = _service()
    relation = service.add(field_1="INFORMATION_TECHNOLOGY", field_2="COMPUTER_APPLICATIONS", role_domain="SOFTWARE",
                           note="  BCA grads  ", actor_id="hr-1", actor_role="HR_ADMIN")
    assert (relation.field_a, relation.field_b, relation.note) == ("COMPUTER_APPLICATIONS", "INFORMATION_TECHNOLOGY",
                                                                   "BCA grads")
    assert audit.log.call_args.kwargs["action_type"].value == "EDUCATION_FIELD_RELATION_ADDED"
    repository.commit.assert_called_once()


@pytest.mark.parametrize("kwargs, error", [
    ({"field_1": "COMPUTER_SCIENCE", "field_2": "COMPUTER_SCIENCE"}, UnprocessableError),
    ({"field_1": "COMPUTER_SCIENCE", "field_2": "ASTROLOGY"}, UnprocessableError),
    ({"field_1": "COMPUTER_SCIENCE", "field_2": "OTHER"}, UnprocessableError),
    ({"field_1": "COMPUTER_SCIENCE", "field_2": "STATISTICS", "role_domain": "SPACE"}, UnprocessableError),
])
def test_invalid_pairs_are_refused(kwargs, error):
    service, repository, audit = _service()
    with pytest.raises(error):
        service.add(**{"role_domain": "SOFTWARE", "note": None, "actor_id": "hr-1", "actor_role": "HR_ADMIN",
                       **kwargs})
    repository.add.assert_not_called()
    audit.log.assert_not_called()


def test_a_pair_already_there_is_a_conflict():
    service, repository, audit = _service(existing=object())
    with pytest.raises(ConflictError):
        service.add(field_1="COMPUTER_SCIENCE", field_2="INFORMATION_TECHNOLOGY", role_domain="SOFTWARE", note=None,
                    actor_id="hr-1", actor_role="HR_ADMIN")
    repository.rollback.assert_called_once()


def test_removing_a_pair_is_audited():
    service, repository, audit = _service()
    repository.get.return_value = SimpleNamespace(field_a="COMPUTER_SCIENCE", field_b="ELECTRONICS_ENGINEERING",
                                                  role_domain="SOFTWARE", note=None)
    relation_id = uuid4()
    service.remove(relation_id, actor_id="hr-1", actor_role="HR_ADMIN")
    repository.delete.assert_called_once()
    assert audit.log.call_args.kwargs["action_type"].value == "EDUCATION_FIELD_RELATION_REMOVED"
    assert audit.log.call_args.kwargs["entity_id"] == relation_id


def test_removing_a_missing_pair_is_not_found():
    service, repository, audit = _service()
    repository.get.return_value = None
    with pytest.raises(NotFoundError):
        service.remove(uuid4(), actor_id="hr-1", actor_role="HR_ADMIN")


def test_ordered_pair():
    assert ordered_pair("B", "A") == ("A", "B") and ordered_pair("A", "B") == ("A", "B")


def test_adding_or_removing_a_pair_marks_the_affected_candidates_out_of_date():
    """Settings phase 3: scored candidates of live campaigns with that role domain are stamped; the audit counts them."""
    from unittest.mock import patch
    from types import SimpleNamespace as NS
    module = "app.services.education.education_field_family_service.mark_after_edit"
    service, repository, audit = _service()
    with patch(module, return_value=4) as mark:
        service.add(field_1="DATA_SCIENCE", field_2="STATISTICS", role_domain="DATA", note=None, actor_id="hr",
                    actor_role="HR_ADMIN")
    assert mark.call_args.args[1] == "education_relation" and mark.call_args.kwargs["role_domain"] == "DATA"
    assert audit.log.call_args.kwargs["details"]["candidates_marked_out_of_date"] == 4
    repository.get.return_value = NS(field_a="DATA_SCIENCE", field_b="STATISTICS", role_domain="DATA", note=None)
    with patch(module, return_value=2) as mark:
        service.remove("rel-1", actor_id="hr", actor_role="HR_ADMIN")
    assert mark.call_args.kwargs["role_domain"] == "DATA"
    assert audit.log.call_args.kwargs["details"]["candidates_marked_out_of_date"] == 2

