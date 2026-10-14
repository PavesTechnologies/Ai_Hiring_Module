"""
Adding a skill the extraction missed and removing one it invented, in
calibration (HR_ADMIN). Each is an HR decision (HR_VERIFIED / audited), gets
the shared follow-up (calibration version, live candidates out of date) and
sends nothing.
"""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.exception_handler.exceptions import ConflictError, NotFoundError, UnprocessableError
from app.models.skills import JDSkillImportance, JDSkillVerificationStatus
from app.schemas.jd.calibration import JDCalibrationAddSkillRequest, JDCalibrationView
from app.services.jd.jd_calibration_skill_service import NEW_SKILL_SOURCE, JDCalibrationSkillService
from app.services.skills.skill_sense_resolver import SenseDef

SERVICE = "app.services.jd.jd_calibration_skill_service"


def _setup():
    jd = SimpleNamespace(id=uuid4(), title="ServiceNow ITSM Consultant")
    incident = SimpleNamespace(id=uuid4(), canonical_name="Incident Management", is_active=True)
    db = MagicMock()
    skills = MagicMock()
    skills.get_skill_by_id.return_value = incident
    skills.get_jd_skill.return_value = None
    skills.find_skill_by_name_or_alias.return_value = None
    skills.create_jd_skill.side_effect = lambda **kw: SimpleNamespace(id=uuid4(), sense_key=None, **kw)
    skills.create_skill_ontology.side_effect = lambda **kw: SimpleNamespace(id=uuid4(), is_active=True, **kw)
    skills.count_core_jd_skills.return_value = 3
    calibration = MagicMock()
    calibration._jd.return_value = jd
    calibration.get_view.return_value = JDCalibrationView.model_construct()
    calibration.repo.load_senses.return_value = [
        SenseDef(incident.id, "ITSM", "IT incident handling", "", ("ticket",)),
    ]
    curation, audit = MagicMock(), MagicMock()
    service = JDCalibrationSkillService(db, skills, curation, calibration, audit)
    return SimpleNamespace(service=service, jd=jd, incident=incident, db=db, skills=skills, curation=curation,
                           audit=audit)


@pytest.fixture
def effects():
    with patch(f"{SERVICE}.apply_jd_skill_change_effects") as apply:
        apply.side_effect = lambda db, jd_ids, **kw: {j: {"calibration_version": 2, "candidates_marked_out_of_date": 3}
                                                      for j in jd_ids}
        yield apply


def _add(s, **payload):
    return s.service.add(s.jd.id, JDCalibrationAddSkillRequest(**payload), actor_id="hr-1", actor_role="HR_ADMIN")


# --- add ---------------------------------------------------------------------

def test_adding_a_catalogue_skill_is_recorded_as_an_hr_decision(effects):
    s = _setup()
    result = _add(s, canonical_skill_id=s.incident.id, tier="CORE", sense_key="ITSM")

    row = s.skills.create_jd_skill.call_args.kwargs
    assert row["canonical_skill_id"] == s.incident.id and row["mandatory"] is True
    assert row["importance"] == JDSkillImportance.CORE
    assert row["verification_status"] == JDSkillVerificationStatus.HR_VERIFIED
    assert row["verified_by"] == "hr-1" and row["verified_at"] is not None and row["match_tier"] == "MANUAL_HR"
    assert s.audit.log.call_args.kwargs["details"]["change"] == "added in calibration"
    assert effects.call_args.args[1] == [s.jd.id]
    assert "'Incident Management' added as core" in effects.call_args.kwargs["note"]
    s.db.commit.assert_called_once()
    s.skills.create_skill_ontology.assert_not_called()
    s.curation.enqueue_skill_embedding.assert_not_called()
    assert result.action == "ADDED" and result.calibration_version == 2 and result.candidates_marked_out_of_date == 3


def test_a_preferred_skill_is_not_mandatory(effects):
    s = _setup()
    _add(s, canonical_skill_id=s.incident.id, tier="PREFERRED")
    row = s.skills.create_jd_skill.call_args.kwargs
    assert row["mandatory"] is False and row["importance"] is None


def test_adding_a_new_skill_puts_it_in_the_catalogue(effects):
    s = _setup()
    result = _add(s, new_skill_name="  ServiceNow ITSM  ", tier="CORE")

    created = s.skills.create_skill_ontology.call_args.kwargs
    assert created == {"canonical_name": "ServiceNow ITSM", "source": NEW_SKILL_SOURCE}
    s.curation.enqueue_skill_embedding.assert_called_once()
    assert s.curation.on_committed.call_args.kwargs["catalog_changed"] is True
    assert result.created_new_skill is True and "(new catalogue skill)" in effects.call_args.kwargs["note"]


def test_a_new_name_already_in_the_catalogue_is_refused(effects):
    s = _setup()
    s.skills.find_skill_by_name_or_alias.return_value = s.incident
    with pytest.raises(ConflictError, match="add it from the catalogue"):
        _add(s, new_skill_name="incident management", tier="CORE")
    s.skills.create_skill_ontology.assert_not_called()
    s.db.rollback.assert_called_once()
    effects.assert_not_called()


def test_a_skill_already_on_the_jd_is_refused(effects):
    s = _setup()
    s.skills.get_jd_skill.return_value = object()
    with pytest.raises(ConflictError, match="already on this JD"):
        _add(s, canonical_skill_id=s.incident.id, tier="CORE")
    s.skills.create_jd_skill.assert_not_called()


def test_a_meaning_must_belong_to_the_skill(effects):
    s = _setup()
    with pytest.raises(UnprocessableError):
        _add(s, canonical_skill_id=s.incident.id, tier="CORE", sense_key="ORGANISATIONAL")
    s.skills.create_jd_skill.assert_not_called()


@pytest.mark.parametrize("payload", [{"tier": "CORE"},
                                     {"tier": "CORE", "canonical_skill_id": uuid4(), "new_skill_name": "X"},
                                     {"tier": "CORE", "new_skill_name": "   "}])
def test_the_request_names_exactly_one_skill(payload):
    with pytest.raises(ValidationError):
        JDCalibrationAddSkillRequest(**payload)


# --- remove ------------------------------------------------------------------

def _row(s, *, core=False):
    return SimpleNamespace(id=uuid4(), jd_id=s.jd.id, canonical_skill_id=uuid4(), mandatory=True,
                           importance=JDSkillImportance.CORE if core else JDSkillImportance.SUPPORTING,
                           match_tier="FUZZY")


def test_removing_an_invented_skill_unlinks_it_and_is_recorded(effects):
    s = _setup()
    row = _row(s)
    s.db.get.return_value = row
    s.skills.get_skill_by_id.return_value = SimpleNamespace(id=row.canonical_skill_id, canonical_name="PagerDuty")

    result = s.service.remove(s.jd.id, row.id, actor_id="hr-1", actor_role="HR_ADMIN")

    s.skills.delete_jd_skill.assert_called_once_with(row)
    audit = s.audit.log.call_args.kwargs
    assert audit["action_type"].value == "JD_SKILL_REMOVED" and audit["details"]["skill"] == "PagerDuty"
    assert "'PagerDuty' removed" in effects.call_args.kwargs["note"]
    s.db.commit.assert_called_once()
    assert result.action == "REMOVED" and result.calibration_version == 2


def test_the_only_core_skill_cannot_be_removed(effects):
    s = _setup()
    s.db.get.return_value = _row(s, core=True)
    s.skills.count_core_jd_skills.return_value = 1
    with pytest.raises(UnprocessableError, match="only core skill"):
        s.service.remove(s.jd.id, uuid4(), actor_id="hr-1", actor_role="HR_ADMIN")
    s.skills.delete_jd_skill.assert_not_called()
    effects.assert_not_called()


def test_a_skill_of_another_jd_is_not_found(effects):
    s = _setup()
    s.db.get.return_value = SimpleNamespace(id=uuid4(), jd_id=uuid4())
    with pytest.raises(NotFoundError):
        s.service.remove(s.jd.id, uuid4(), actor_id="hr-1", actor_role="HR_ADMIN")
    s.skills.delete_jd_skill.assert_not_called()
