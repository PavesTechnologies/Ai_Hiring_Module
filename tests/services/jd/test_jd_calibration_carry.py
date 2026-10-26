"""A new JD version carries the previous calibration forward as a draft for HR to re-confirm."""
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from app.models.jd.job_descriptions import JDCalibrationStatus, JDEducationRequirement
from app.models.skills import JDSkill, JDSkillImportance, JDSkillVerificationStatus, JDUnknownSkill
from app.services.jd.jd_calibration_carry import carry_settings, carry_skill_calibration
from tests.services.jd.test_jd_calibration_service import FakeRepo, _jd, _setup, _skill
from tests.services.jd.test_jd_service_update_jd_embed_jd_hook import (
    _make_existing_jd, _make_request, _make_service,
)

DATA_ENG, ML, SPARK = uuid4(), uuid4(), uuid4()


def _calibrated(jd, **overrides):
    values = dict(calibration_status=JDCalibrationStatus.CALIBRATED, calibration_version=3, calibration_draft_from=None,
                  calibrated_min_experience_years=12, experience_tolerance_years=2,
                  education_requirement=JDEducationRequirement.PREFERRED, internship_credit_pct=0,
                  role_domain="DATA", profile_template="EXPERIENCED", min_experience_years=1.0)
    values.update(overrides)
    for key, value in values.items():
        setattr(jd, key, value)
    return jd


def _row(skill_id, tier="CORE", **kw):
    return SimpleNamespace(
        canonical_skill_id=skill_id, mandatory=tier != "PREFERRED",
        importance=JDSkillImportance(tier) if tier in ("CORE", "SUPPORTING") else None,
        weight=1.0, alias_skill_ids=None, confidence=0.9, sense_key=kw.get("sense_key"),
        required=kw.get("required", False), requirement_text=kw.get("requirement_text"),
        verified_by="hr-1", verified_at=None, match_tier="EXACT",
        verification_status=JDSkillVerificationStatus.HR_VERIFIED)


def _update(existing_jd, *, skills=(), links=(), request=None):
    service, repository = _make_service()
    repository.get_by_id.return_value = existing_jd
    repository.jd_skill_rows.return_value = list(skills)
    repository.unknown_skill_link_rows.return_value = list(links)
    with patch("app.services.jd.jd_service.validate_prompt_template_selection",
               return_value=SimpleNamespace(name="Standard")):
        service.update_jd(existing_jd.id, request or _make_request(), updated_by="hr-1")
    new_jd = repository.create_job_description.call_args.args[0]
    [added] = [c.args[0] for c in repository.add_all.call_args_list]
    return new_jd, added, service.audit_service.log.call_args.kwargs["details"]


# --- a details edit -----------------------------------------------------------------

def test_a_details_edit_carries_the_skills_and_settings_as_a_draft():
    existing = _calibrated(_make_existing_jd())
    data_eng = _row(DATA_ENG, required=True, requirement_text="Batch and streaming pipelines")
    spark = _row(SPARK, tier="SUPPORTING")
    link = SimpleNamespace(unknown_skill_id=uuid4(), mandatory=True, status="RESOLVED", resolution="NEW_SKILL",
                           resolved_canonical_skill_id=DATA_ENG, resolved_by="hr-1", resolved_at=None)
    new_jd, added, audit_details = _update(existing, skills=[data_eng, spark], links=[link])

    assert new_jd.calibration_status == JDCalibrationStatus.NOT_CALIBRATED and new_jd.calibration_version == 0
    assert new_jd.calibration_draft_from == existing.id
    assert (new_jd.profile_template, new_jd.role_domain, new_jd.education_requirement, new_jd.internship_credit_pct,
            new_jd.experience_tolerance_years) == ("EXPERIENCED", "DATA", JDEducationRequirement.PREFERRED, 0, 2)
    assert new_jd.calibrated_min_experience_years == 12              # the JD's own minimum is unchanged
    skills = [r for r in added if isinstance(r, JDSkill)]
    links = [r for r in added if isinstance(r, JDUnknownSkill)]
    copied = {r.canonical_skill_id: r for r in skills}
    assert all(r.jd_id == new_jd.id for r in added)
    assert copied[DATA_ENG].importance == JDSkillImportance.CORE and copied[DATA_ENG].required is True
    assert copied[DATA_ENG].requirement_text == "Batch and streaming pipelines"
    assert copied[SPARK].importance == JDSkillImportance.SUPPORTING
    assert len(links) == 1 and links[0].resolution == "NEW_SKILL"
    assert audit_details["calibration_draft_from"] == str(existing.id)     # the audit entry names the origin


def test_a_changed_minimum_drops_the_calibrated_minimum():
    existing = _calibrated(_make_existing_jd(), min_experience_years=3.0)   # the request sets 1.0
    new_jd, *_ = _update(existing)
    assert new_jd.calibrated_min_experience_years is None and new_jd.calibration_draft_from == existing.id


def test_an_uncalibrated_jd_keeps_its_skills_but_has_no_draft():
    existing = _make_existing_jd()                                       # never calibrated
    new_jd, added, _ = _update(existing, skills=[_row(DATA_ENG)])
    assert getattr(new_jd, "calibration_draft_from", None) is None
    assert [r.canonical_skill_id for r in added] == [DATA_ENG]           # the skills no longer vanish


def test_a_draft_edited_again_still_carries():
    previous = uuid4()
    existing = _calibrated(_make_existing_jd(), calibration_status=JDCalibrationStatus.NOT_CALIBRATED,
                           calibration_draft_from=previous)
    new_jd, *_ = _update(existing)
    assert new_jd.calibration_draft_from == existing.id and new_jd.profile_template == "EXPERIENCED"


# --- new text -----------------------------------------------------------------------

def test_new_text_keeps_the_tiers_of_skills_still_there_but_not_the_requirement_text():
    old = [_row(DATA_ENG, required=True, requirement_text="Old wording"), _row(ML, sense_key="AI"),
           _row(SPARK, tier="SUPPORTING")]
    fresh = [JDSkill(canonical_skill_id=DATA_ENG, mandatory=True, importance=JDSkillImportance.SUPPORTING),
             JDSkill(canonical_skill_id=SPARK, mandatory=True, importance=JDSkillImportance.CORE),
             JDSkill(canonical_skill_id=uuid4(), mandatory=False, importance=None)]       # new on this version
    carry_skill_calibration(old, fresh)
    assert (fresh[0].importance, fresh[0].required, fresh[0].requirement_text) == (
        JDSkillImportance.CORE, True, None)
    assert fresh[1].importance == JDSkillImportance.SUPPORTING
    assert fresh[2].importance is None and not fresh[2].mandatory


def test_new_text_with_a_new_extracted_minimum_drops_the_calibrated_minimum():
    old = _calibrated(SimpleNamespace(id=uuid4(), extracted_json={"experience": {"min_experience_years": 10}}))
    new = SimpleNamespace(extracted_json={"experience": {"min_experience_years": 8}}, min_experience_years=1.0)
    carry_settings(old, new)
    assert new.calibrated_min_experience_years is None and new.role_domain == "DATA"


# --- the calibration view -----------------------------------------------------------

class DraftRepo(FakeRepo):
    def __init__(self, jd, skills, previous, previous_skills):
        super().__init__(jd, skills)
        self.previous, self.previous_skills = previous, previous_skills

    def get_jd(self, jd_id):
        return self.previous if jd_id == self.previous.id else super().get_jd(jd_id)

    def jd_skills_with_names(self, jd_id):
        return list(self.previous_skills) if jd_id == self.previous.id else list(self.skills)


def _draft_service(**jd_overrides):
    service, repo, audit, jd, change, python, aws = _setup()
    previous = _jd(version_number=4)
    jd.calibration_draft_from, jd.calibrated_min_experience_years = previous.id, 12
    for key, value in jd_overrides.items():
        setattr(jd, key, value)
    dropped = _skill(uuid4())
    draft_repo = DraftRepo(jd, repo.skills, previous, [*repo.skills, (dropped, "Kafka")])
    return type(service)(draft_repo, audit), jd


def test_the_view_names_the_draft_origin_the_dropped_skills_and_the_carried_minimum():
    service, jd = _draft_service()
    view = service.get_view(jd.id, can_calibrate=True)
    assert view.calibration_status == "NOT_CALIBRATED"
    assert view.calibration_draft_from_version == 4 and view.calibration_draft_dropped_skills == ["Kafka"]
    assert view.min_experience_years == 12.0


def test_once_calibrated_the_view_shows_no_draft():
    service, jd = _draft_service(calibration_status=JDCalibrationStatus.CALIBRATED)
    view = service.get_view(jd.id, can_calibrate=True)
    assert view.calibration_draft_from_version is None and view.calibration_draft_dropped_skills == []


# --- schema -------------------------------------------------------------------------

def test_the_column_is_a_nullable_self_reference_added_by_an_additive_migration():
    from pathlib import Path

    from alembic.config import Config
    from alembic.script import ScriptDirectory

    from app.models.jd.job_descriptions import JobDescription
    column = JobDescription.__table__.c.calibration_draft_from
    assert column.nullable and {fk.column.table.name for fk in column.foreign_keys} == {"job_descriptions"}
    root = Path(__file__).resolve().parents[3]
    script = ScriptDirectory.from_config(Config(str(root / "alembic.ini")))
    assert len(script.get_heads()) == 1
    revision = script.get_revision("e3a5c7e9f1b2")
    assert revision.down_revision == "d2f4a6c8e0b1"
    assert '"calibration_draft_from"' in Path(revision.path).read_text(encoding="utf-8")


def test_reprocessing_a_calibrated_jd_carries_tiers_and_settings_into_the_new_version():
    from tests.services.jd.test_jd_service_weight_assignment import (
        _created_jd_skills, _match, _persist, make_jd_service,
    )
    service, repository = make_jd_service()
    existing_id = uuid4()
    existing = _calibrated(SimpleNamespace(id=existing_id, prompt_template_id=uuid4(), extracted_json={}),
                           min_experience_years=None)
    repository.has_active_campaign.return_value = False
    repository.get_by_id.return_value = existing
    repository.jd_skill_rows.return_value = [_row(DATA_ENG, required=True, requirement_text="Old wording")]
    _, skill_repository = _persist(service, [_match("Data Engineering", True, DATA_ENG), _match("Kafka", True)],
                                   existing_jd_id=existing_id, version_number=2, parent_jd_id=existing_id,
                                   lineage_root_id=existing_id)
    new_jd = repository.create_job_description.call_args.args[0]
    assert new_jd.calibration_draft_from == existing_id and new_jd.profile_template == "EXPERIENCED"
    assert new_jd.calibration_status == JDCalibrationStatus.NOT_CALIBRATED
    by_id = {s.canonical_skill_id: s for s in _created_jd_skills(skill_repository)}
    assert by_id[DATA_ENG].importance == JDSkillImportance.CORE and by_id[DATA_ENG].required is True
    assert by_id[DATA_ENG].requirement_text is None                   # the new wording refills it
    repository.jd_skill_rows.assert_called_once_with(existing_id)
