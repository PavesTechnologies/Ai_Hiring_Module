"""JD calibration (Phase 2 Step 1): view, validation, save, versioning, out-of-date marking, exemption end."""
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from app.exception_handler.exceptions import UnprocessableError
from app.models.campaigns import CampaignStatus
from app.models.jd.job_descriptions import JDCalibrationStatus, JDEducationRequirement, JDVerificationStatus
from app.models.skills import JDSkillImportance, JDSkillVerificationStatus, JDUnknownSkillStatus
from app.schemas.jd.calibration import JDCalibrationSaveRequest
from app.services.jd.jd_calibration_service import JDCalibrationService
from app.services.skills.skill_sense_resolver import SenseDef

SERVICE = "app.services.jd.jd_calibration_service"
CHANGE_MGMT, PYTHON, AWS = uuid4(), uuid4(), uuid4()


def _jd(**overrides):
    values = dict(
        id=uuid4(), title="AI transformation lead", raw_text="Lead AI adoption across the business.",
        version_number=1, extracted_json={"experience": {"min_experience_years": 10}},
        min_experience_years=10, calibrated_min_experience_years=None, experience_tolerance_years=None,
        education_requirement=JDEducationRequirement.MANDATORY,
        education_criteria={"degree": "Bachelor", "field": "Computer Science"},
        calibration_status=JDCalibrationStatus.NOT_CALIBRATED, calibration_version=0,
        calibrated_by=None, calibrated_at=None, is_verified=JDVerificationStatus.PARTIALLY_VERIFIED,
        updated_at=None,
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def _skill(canonical_id, mandatory=True, importance=JDSkillImportance.CORE, sense_key=None):
    return SimpleNamespace(id=uuid4(), canonical_skill_id=canonical_id, mandatory=mandatory, importance=importance,
                           sense_key=sense_key, verification_status=JDSkillVerificationStatus.PENDING_REVIEW,
                           verified_by=None, verified_at=None)


class FakeRepo:
    def __init__(self, jd, skills, campaigns=(), unknown=()):
        self.jd, self.skills, self.campaigns, self.unknown = jd, skills, list(campaigns), list(unknown)
        self.versions, self.committed, self.rolled_back = [], 0, 0
        self.db = MagicMock()

    def get_jd(self, jd_id):
        return self.jd if jd_id == self.jd.id else None

    def jd_skills_with_names(self, jd_id):
        return list(self.skills)

    def unknown_skill_links(self, jd_id):
        return list(self.unknown)

    def load_senses(self):
        return [
            SenseDef(CHANGE_MGMT, "ITSM", "IT service change control", "CAB, release", ("cab", "itil")),
            SenseDef(CHANGE_MGMT, "ORGANISATIONAL", "People side of change", "adoption", ("adoption",), is_default=True),
        ]

    def campaigns_for_jd(self, jd_id):
        return list(self.campaigns)

    def user_names(self, ids):
        return {"hr-1": "Jordan HR"}

    def skill_names(self, ids):
        return {i: "Mapped skill" for i in ids}

    def latest_version(self, jd_id):
        return max(self.versions, key=lambda v: v.version_no, default=None)

    def latest_version_no(self, jd_id):
        return max((v.version_no for v in self.versions), default=0)

    def add_version(self, version):
        self.versions.append(version)

    def list_versions(self, jd_id):
        return sorted(self.versions, key=lambda v: -v.version_no)

    def flush(self):
        pass

    def commit(self):
        self.committed += 1

    def rollback(self):
        self.rolled_back += 1


def _setup(campaigns=(), unknown=(), **jd_overrides):
    jd = _jd(**jd_overrides)
    change = _skill(CHANGE_MGMT)
    python = _skill(PYTHON, importance=JDSkillImportance.SUPPORTING)
    aws = _skill(AWS, mandatory=False, importance=None)
    repo = FakeRepo(jd, [(change, "Change Management"), (python, "Python"), (aws, "AWS")], campaigns, unknown)
    audit = MagicMock()
    return JDCalibrationService(repo, audit), repo, audit, jd, change, python, aws


def _request(change, python, aws, *, change_tier="CORE", sense="ORGANISATIONAL", python_tier="SUPPORTING",
             aws_tier="PREFERRED", **kw):
    return JDCalibrationSaveRequest(
        skills=[{"jd_skill_id": change.id, "tier": change_tier, "sense_key": sense},
                {"jd_skill_id": python.id, "tier": python_tier},
                {"jd_skill_id": aws.id, "tier": aws_tier}],
        min_experience_years=kw.get("min_years", 10), experience_tolerance_years=kw.get("tolerance"),
        education_requirement=kw.get("education", "MANDATORY"))


@pytest.fixture(autouse=True)
def no_db_update():
    with patch(f"{SERVICE}.mark_campaigns_out_of_date", return_value=4) as mark:
        yield mark


# --- view ----------------------------------------------------------------------

def test_view_shows_tiers_ambiguous_skill_options_and_verification_as_a_detail():
    unknown_link = SimpleNamespace(mandatory=True, status=JDUnknownSkillStatus.PENDING)
    unknown_skill = SimpleNamespace(id=uuid4(), raw_text="Agentic workflows")
    service, *_ = _setup(unknown=[(unknown_link, unknown_skill)])

    view = service.get_view(service.repo.jd.id, can_calibrate=False)

    by_name = {s.name: s for s in view.skills}
    assert (by_name["Change Management"].tier, by_name["Python"].tier, by_name["AWS"].tier) == (
        "CORE", "SUPPORTING", "PREFERRED")
    assert by_name["Change Management"].ambiguous and len(by_name["Change Management"].sense_options) == 2
    assert by_name["Change Management"].suggested_sense_basis == "cue"      # "adoption" in the JD text
    assert not by_name["Python"].ambiguous and by_name["Python"].sense_options == []
    assert view.calibration_status == "NOT_CALIBRATED"
    assert view.verification_status == "PARTIALLY_VERIFIED" and view.pending_unknown_skill_count == 1
    assert view.default_experience_tolerance_years == 1.0                    # max(1, 10% of 10)
    assert view.can_calibrate is False
    assert any("Choose the meaning of 'Change Management'" in w for w in view.warnings)


# --- validation ----------------------------------------------------------------

def test_an_ambiguous_skill_needs_a_sense():
    service, repo, _, jd, change, python, aws = _setup()
    with pytest.raises(UnprocessableError, match="meaning of each ambiguous skill"):
        service.save(jd.id, _request(change, python, aws, sense=None), actor_id="hr-1", actor_role="HR_ADMIN")
    assert repo.committed == 0 and repo.rolled_back == 1


def test_an_invalid_sense_is_refused():
    service, _, _, jd, change, python, aws = _setup()
    with pytest.raises(UnprocessableError):
        service.save(jd.id, _request(change, python, aws, sense="NOT_A_SENSE"), actor_id="hr-1", actor_role="HR_ADMIN")


def test_every_skill_must_be_confirmed_and_at_least_one_core():
    service, _, _, jd, change, python, aws = _setup()
    partial = JDCalibrationSaveRequest(skills=[{"jd_skill_id": change.id, "tier": "CORE", "sense_key": "ITSM"}])
    with pytest.raises(UnprocessableError, match="missing: AWS, Python"):
        service.save(jd.id, partial, actor_id="hr-1", actor_role="HR_ADMIN")
    with pytest.raises(UnprocessableError, match="at least one core skill"):
        service.save(jd.id, _request(change, python, aws, change_tier="SUPPORTING"), actor_id="hr-1",
                     actor_role="HR_ADMIN")


def test_more_than_five_core_skills_warns_but_does_not_block():
    service, repo, _, jd, *_ = _setup()
    extra = [(_skill(uuid4()), f"Skill {i}") for i in range(5)]
    repo.skills = repo.skills[:1] + extra
    change = repo.skills[0][0]
    request = JDCalibrationSaveRequest(
        skills=[{"jd_skill_id": change.id, "tier": "CORE", "sense_key": "ITSM"}]
               + [{"jd_skill_id": row.id, "tier": "CORE"} for row, _ in extra],
        min_experience_years=10)
    result = service.save(jd.id, request, actor_id="hr-1", actor_role="HR_ADMIN")
    assert result.changed and any("6 core skills" in w for w in result.warnings)


# --- save ----------------------------------------------------------------------

def test_first_calibration_sets_status_who_when_version_and_skill_settings():
    service, repo, audit, jd, change, python, aws = _setup()

    result = service.save(jd.id, _request(change, python, aws, python_tier="CORE", tolerance=2,
                                          education="PREFERRED", min_years=8),
                          actor_id="hr-1", actor_role="HR_ADMIN")

    assert result.changed and result.calibration_version == 1
    assert jd.calibration_status == JDCalibrationStatus.CALIBRATED
    assert jd.calibrated_by == "hr-1" and isinstance(jd.calibrated_at, datetime)
    assert change.sense_key == "ORGANISATIONAL"
    assert python.importance == JDSkillImportance.CORE and python.mandatory is True
    assert aws.mandatory is False and aws.importance is None
    assert jd.calibrated_min_experience_years == 8
    assert jd.experience_tolerance_years == 2
    assert jd.education_requirement == JDEducationRequirement.PREFERRED
    assert [v.version_no for v in repo.versions] == [1]
    assert audit.log.call_args.kwargs["action_type"].value == "JD_CALIBRATED"
    assert repo.committed == 1


def test_calibration_never_changes_the_jds_extraction():
    service, repo, audit, jd, change, python, aws = _setup()
    extracted_before = {"experience": {"min_experience_years": 10}}

    service.save(jd.id, _request(change, python, aws, min_years=8), actor_id="hr-1", actor_role="HR_ADMIN")

    assert jd.extracted_json == extracted_before          # extraction kept as extracted
    assert jd.min_experience_years == 10                  # the extracted column too
    assert jd.calibrated_min_experience_years == 8        # the calibrated value lives beside it
    view = service.get_view(jd.id, can_calibrate=True)
    assert view.min_experience_years == 8                 # what scoring will use
    assert view.extracted_min_experience_years == 10      # the hint: what a blank minimum falls back to


def test_skills_confirmed_in_calibration_are_recorded_as_verified_by_hr():
    service, repo, audit, jd, change, python, aws = _setup()

    service.save(jd.id, _request(change, python, aws), actor_id="hr-1", actor_role="HR_ADMIN")

    for skill in (change, python, aws):
        assert skill.verification_status == JDSkillVerificationStatus.HR_VERIFIED
        assert skill.verification_status != JDSkillVerificationStatus.AUTO_VERIFIED
        assert skill.verified_by == "hr-1"
        assert isinstance(skill.verified_at, datetime) and skill.verified_at == jd.calibrated_at


def test_saving_the_same_calibration_again_creates_no_new_version():
    service, repo, audit, jd, change, python, aws = _setup()
    service.save(jd.id, _request(change, python, aws), actor_id="hr-1", actor_role="HR_ADMIN")
    audit.reset_mock()

    again = service.save(jd.id, _request(change, python, aws), actor_id="hr-1", actor_role="HR_ADMIN")

    assert again.changed is False and again.calibration_version == 1
    assert len(repo.versions) == 1
    audit.log.assert_not_called()


def test_changing_a_calibrated_jd_used_by_a_live_campaign_versions_and_marks_out_of_date(no_db_update):
    live = SimpleNamespace(id=uuid4(), name="Live", status=CampaignStatus.ACTIVE, jd_calibration_exempt=False)
    paused = SimpleNamespace(id=uuid4(), name="Paused", status=CampaignStatus.PAUSED, jd_calibration_exempt=False)
    closed = SimpleNamespace(id=uuid4(), name="Closed", status=CampaignStatus.CLOSED, jd_calibration_exempt=False)
    service, repo, audit, jd, change, python, aws = _setup(campaigns=[live, paused, closed])
    service.save(jd.id, _request(change, python, aws), actor_id="hr-1", actor_role="HR_ADMIN")
    no_db_update.reset_mock()

    result = service.save(jd.id, _request(change, python, aws, sense="ITSM"), actor_id="hr-2", actor_role="HR_ADMIN")

    assert result.calibration_version == 2 and result.candidates_marked_out_of_date == 4
    campaign_ids = no_db_update.call_args.args[1]
    assert set(campaign_ids) == {live.id, paused.id}                # live campaigns only; scores never changed
    assert "calibration changed (version 2)" in no_db_update.call_args.args[2]
    assert repo.versions[-1].changes == {"skills": [
        {"skill": "Change Management", "field": "sense_key", "before": "ORGANISATIONAL", "after": "ITSM"}]}
    assert audit.log.call_args.kwargs["action_type"].value == "JD_CALIBRATION_CHANGED"


def test_calibrating_ends_the_exemption_of_the_jds_campaigns():
    exempt = SimpleNamespace(id=uuid4(), name="Old", status=CampaignStatus.ACTIVE, jd_calibration_exempt=True)
    service, _, _, jd, change, python, aws = _setup(campaigns=[exempt])

    service.save(jd.id, _request(change, python, aws), actor_id="hr-1", actor_role="HR_ADMIN")

    assert exempt.jd_calibration_exempt is False


def test_versions_are_listed_newest_first_with_names():
    service, _, _, jd, change, python, aws = _setup()
    service.save(jd.id, _request(change, python, aws), actor_id="hr-1", actor_role="HR_ADMIN")
    service.save(jd.id, _request(change, python, aws, sense="ITSM"), actor_id="hr-1", actor_role="HR_ADMIN")

    versions = service.list_versions(jd.id)

    assert [v.version_no for v in versions] == [2, 1]
    assert versions[0].calibrated_by_name == "Jordan HR"


def test_an_unknown_jd_is_not_found():
    from app.exception_handler.exceptions import NotFoundError
    service, *_ = _setup()
    with pytest.raises(NotFoundError):
        service.get_view(uuid4(), can_calibrate=True)


def test_nothing_in_calibration_sends_email():
    with patch("app.services.notifications.ses_email_client.SESEmailClient.send_email",
               side_effect=AssertionError("email sent")) as send:
        service, _, _, jd, change, python, aws = _setup(
            campaigns=[SimpleNamespace(id=uuid4(), name="Live", status=CampaignStatus.ACTIVE,
                                       jd_calibration_exempt=False)])
        service.save(jd.id, _request(change, python, aws), actor_id="hr-1", actor_role="HR_ADMIN")
        service.save(jd.id, _request(change, python, aws, sense="ITSM"), actor_id="hr-1", actor_role="HR_ADMIN")
    send.assert_not_called()



# --- unknown skills (Phase 2 Step 2) --------------------------------------------

def _unknown(raw_text, mandatory=True, status=JDUnknownSkillStatus.PENDING):
    return (SimpleNamespace(mandatory=mandatory, status=status),
            SimpleNamespace(id=uuid4(), raw_text=raw_text, normalized_key=None))


def test_an_unresolved_core_unknown_skill_blocks_calibration():
    extraction = {"experience": {"min_experience_years": 10},
                  "required_skills": {"core": ["Agentic Workflows"], "supporting": ["Prompt Design"]}}
    service, repo, audit, jd, change, python, aws = _setup(
        unknown=[_unknown("Agentic Workflows"), _unknown("Prompt Design")], extracted_json=extraction)

    view = service.get_view(jd.id, can_calibrate=True)
    assert view.blocking_unknown_skills == ["Agentic Workflows"]
    assert [u.blocks_calibration for u in view.unknown_skills] == [True, False]

    with pytest.raises(UnprocessableError, match="Agentic Workflows"):
        service.save(jd.id, _request(change, python, aws), actor_id="hr-1", actor_role="HR_ADMIN")
    assert repo.versions == [] and jd.calibration_status == JDCalibrationStatus.NOT_CALIBRATED


def test_supporting_or_resolved_unknown_skills_do_not_block():
    extraction = {"required_skills": {"core": ["Agentic Workflows"]}}
    resolved = _unknown("Agentic Workflows", status=JDUnknownSkillStatus.RESOLVED)
    service, repo, audit, jd, change, python, aws = _setup(
        unknown=[resolved, _unknown("Prompt Design")], extracted_json=extraction)

    result = service.save(jd.id, _request(change, python, aws), actor_id="hr-1", actor_role="HR_ADMIN")
    assert result.changed and result.calibration.blocking_unknown_skills == []


def test_a_skill_added_by_resolving_an_unknown_skill_is_saved_as_a_calibration_version():
    service, repo, audit, jd, change, python, aws = _setup()
    service.save(jd.id, _request(change, python, aws), actor_id="hr-1", actor_role="HR_ADMIN")
    audit.reset_mock()
    mapped = _skill(uuid4(), importance=JDSkillImportance.SUPPORTING)
    repo.skills.append((mapped, "Prompt Engineering"))

    version = service.record_skill_change(jd.id, actor_id="hr-2", actor_role="HR_ADMIN",
                                          note="unknown skill 'prompt design' mapped to 'Prompt Engineering'")

    assert version == 2 and jd.calibration_version == 2
    latest = repo.versions[-1]
    assert latest.calibrated_by == "hr-2"
    assert latest.changes["skills"] == [{"skill": "Prompt Engineering", "field": "added", "before": None,
                                         "after": "SUPPORTING"}]
    assert "mapped to 'Prompt Engineering'" in latest.changes["note"]
    assert audit.log.call_args.kwargs["action_type"].value == "JD_CALIBRATION_CHANGED"
    assert repo.committed == 1                     # only the save committed; the caller owns this transaction


def test_an_uncalibrated_jd_records_no_version():
    service, repo, audit, jd, *_ = _setup()
    assert service.record_skill_change(jd.id, actor_id="hr-1", actor_role="HR_ADMIN", note="x") is None
    assert repo.versions == []


def test_a_skill_removed_in_calibration_is_saved_as_a_calibration_version():
    service, repo, audit, jd, change, python, aws = _setup()
    service.save(jd.id, _request(change, python, aws), actor_id="hr-1", actor_role="HR_ADMIN")
    repo.skills = [(change, "Change Management"), (python, "Python")]          # AWS removed

    version = service.record_skill_change(jd.id, actor_id="hr-1", actor_role="HR_ADMIN", note="'AWS' removed")

    assert version == 2
    assert repo.versions[-1].changes["skills"] == [{"skill": "AWS", "field": "removed", "before": "PREFERRED",
                                                    "after": None}]
