"""
Resolving a JD's unknown skills (Phase 2 Step 2): MAP and DROP touch this JD
only; NEW is global (the catalogue). Every JD resolved gets the shared
follow-up: verification, calibration version, live candidates out of date.
"""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from app.exception_handler.exceptions import ConflictError, NotFoundError, UnprocessableError
from app.models.skills import (
    JDSkillImportance, JDSkillVerificationStatus, JDUnknownSkillResolution, JDUnknownSkillStatus,
)
from app.schemas.jd.calibration import JDCalibrationView, JDUnknownSkillResolveRequest
from app.services.jd.jd_skill_change_effects import apply_jd_skill_change_effects
from app.services.jd.jd_unknown_skill_service import SUGGESTION_COUNT, JDUnknownSkillService

SERVICE = "app.services.jd.jd_unknown_skill_service"
EFFECTS = "app.services.jd.jd_skill_change_effects"


def _setup(*, link_status=JDUnknownSkillStatus.PENDING, mandatory=True, has_skill=False):
    jd = SimpleNamespace(id=uuid4(), title="AI transformation lead",
                         extracted_json={"required_skills": {"core": ["Agentic Workflows"]}})
    unknown = SimpleNamespace(id=uuid4(), raw_text="Agentic Workflows", normalized_key=None)
    link = SimpleNamespace(jd_id=jd.id, mandatory=mandatory, status=link_status, resolution=None,
                           resolved_canonical_skill_id=None, resolved_by=None, resolved_at=None)
    db = MagicMock()
    db.query.return_value.join.return_value.filter.return_value.first.return_value = (link, unknown)
    target = SimpleNamespace(id=uuid4(), canonical_name="Agentic AI", is_active=True)
    skill_repository = MagicMock()
    skill_repository.get_skill_by_id.return_value = target
    skill_repository.get_jd_skill.return_value = object() if has_skill else None
    calibration = MagicMock()
    calibration._jd.return_value = jd
    calibration.get_view.return_value = JDCalibrationView.model_construct()
    curation = MagicMock()
    service = JDUnknownSkillService(db, skill_repository, curation, calibration, MagicMock(),
                                    embedding_service=MagicMock())
    return SimpleNamespace(service=service, jd=jd, unknown=unknown, link=link, db=db, target=target,
                           skills=skill_repository, curation=curation)


@pytest.fixture
def effects():
    with patch(f"{SERVICE}.apply_jd_skill_change_effects") as apply:
        apply.side_effect = lambda db, jd_ids, **kw: {
            j: {"calibration_version": 3, "candidates_marked_out_of_date": 4} for j in jd_ids}
        yield apply


def _resolve(s, **payload):
    return s.service.resolve(s.jd.id, s.unknown.id, JDUnknownSkillResolveRequest(**payload),
                             actor_id="hr-1", actor_role="HR_ADMIN")


def test_map_adds_the_skill_to_this_jd_only_as_verified_by_hr(effects):
    s = _setup()
    result = _resolve(s, action="MAP", canonical_skill_id=s.target.id)

    kwargs = s.skills.create_jd_skill.call_args.kwargs
    assert kwargs["jd_id"] == s.jd.id and kwargs["canonical_skill_id"] == s.target.id
    assert kwargs["importance"] == JDSkillImportance.CORE and kwargs["mandatory"] is True
    assert kwargs["verification_status"] == JDSkillVerificationStatus.HR_VERIFIED
    assert kwargs["verified_by"] == "hr-1" and kwargs["verified_at"] is not None
    assert s.link.status == JDUnknownSkillStatus.RESOLVED
    assert s.link.resolution == JDUnknownSkillResolution.MAPPED
    assert s.link.resolved_canonical_skill_id == s.target.id and s.link.resolved_by == "hr-1"
    assert effects.call_args.args[1] == [s.jd.id]                 # this JD only
    assert "mapped to 'Agentic AI'" in effects.call_args.kwargs["note"]
    s.db.commit.assert_called_once()
    s.curation.on_committed.assert_called_once_with({s.jd.id}, stale_jd_ids={s.jd.id})
    s.curation.create_canonical_skill_from_unknown.assert_not_called()
    assert result.affected_jd_ids == [s.jd.id] and result.candidates_marked_out_of_date == 4


def test_map_to_a_skill_the_jd_already_has_just_resolves(effects):
    s = _setup(has_skill=True)
    _resolve(s, action="MAP", canonical_skill_id=s.target.id)
    s.skills.create_jd_skill.assert_not_called()
    assert s.link.resolution == JDUnknownSkillResolution.MAPPED
    s.curation.on_committed.assert_called_once_with(set(), stale_jd_ids={s.jd.id})


def test_a_preferred_unknown_skill_maps_as_preferred(effects):
    s = _setup(mandatory=False)
    _resolve(s, action="MAP", canonical_skill_id=s.target.id)
    kwargs = s.skills.create_jd_skill.call_args.kwargs
    assert kwargs["mandatory"] is False and kwargs["importance"] is None


def test_drop_removes_it_from_this_jd_only(effects):
    s = _setup()
    _resolve(s, action="DROP")
    s.skills.create_jd_skill.assert_not_called()
    assert s.link.status == JDUnknownSkillStatus.RESOLVED and s.link.resolution == JDUnknownSkillResolution.DROPPED
    assert effects.call_args.args[1] == [s.jd.id]
    s.db.commit.assert_called_once()


def test_approve_as_new_is_global_through_the_catalogue_path(effects):
    s = _setup()
    other_jd = uuid4()
    s.skills.get_pending_jd_links.return_value = [SimpleNamespace(jd_id=s.jd.id), SimpleNamespace(jd_id=other_jd)]
    s.curation.create_canonical_skill_from_unknown.return_value = {"skill": SimpleNamespace(id=uuid4())}

    def fake_create(*args, **kwargs):
        s.curation.last_change_effects = {s.jd.id: {"candidates_marked_out_of_date": 2},
                                          other_jd: {"candidates_marked_out_of_date": 5}}
        return {"skill": SimpleNamespace(id=uuid4())}
    s.curation.create_canonical_skill_from_unknown.side_effect = fake_create

    result = _resolve(s, action="NEW")

    args, kwargs = s.curation.create_canonical_skill_from_unknown.call_args
    assert args == (s.unknown.id, "hr-1") and kwargs["canonical_name"] == "Agentic Workflows"
    assert result.affected_jd_ids == [s.jd.id, other_jd] and result.candidates_marked_out_of_date == 7
    effects.assert_not_called()          # the catalogue path runs the follow-up for every JD itself
    s.db.commit.assert_not_called()


def test_an_already_resolved_skill_is_refused(effects):
    s = _setup(link_status=JDUnknownSkillStatus.RESOLVED)
    with pytest.raises(ConflictError):
        _resolve(s, action="DROP")
    effects.assert_not_called()


def test_an_unknown_skill_not_on_the_jd_is_not_found(effects):
    s = _setup()
    s.db.query.return_value.join.return_value.filter.return_value.first.return_value = None
    with pytest.raises(NotFoundError):
        _resolve(s, action="DROP")


def test_map_needs_a_catalogue_skill(effects):
    s = _setup()
    with pytest.raises(UnprocessableError):
        _resolve(s, action="MAP")
    s.db.rollback.assert_called_once()
    s.db.commit.assert_not_called()


def test_suggestions_are_the_top_3_by_similarity_for_pending_skills_only():
    s = _setup()
    pending = (SimpleNamespace(status=JDUnknownSkillStatus.PENDING), SimpleNamespace(id=uuid4(), raw_text="Agentic"))
    resolved = (SimpleNamespace(status=JDUnknownSkillStatus.RESOLVED), SimpleNamespace(id=uuid4(), raw_text="Old"))
    s.service.calibration_service.repo.unknown_skill_links.return_value = [pending, resolved]
    s.skills.find_top_similar_canonical_skills.return_value = [
        (SimpleNamespace(id=uuid4(), canonical_name=f"Skill {i}"), 0.9 - i / 10) for i in range(3)]

    result = s.service.suggestions(s.jd.id)

    assert [r.unknown_skill_id for r in result] == [pending[1].id]
    assert len(result[0].suggestions) == 3 and result[0].suggestions[0].similarity == 0.9
    assert s.skills.find_top_similar_canonical_skills.call_args.kwargs["limit"] == SUGGESTION_COUNT == 3


# --- the shared follow-up --------------------------------------------------------

def test_every_resolved_jd_gets_verification_a_calibration_version_and_out_of_date_marking():
    jd_a, jd_b = uuid4(), uuid4()
    skill_repository = MagicMock()
    calibration = MagicMock()
    calibration.record_skill_change.side_effect = lambda jd_id, **kw: 5 if jd_id == jd_a else None
    calibration.repo.get_jd.side_effect = lambda jd_id: SimpleNamespace(title=f"JD {jd_id}")
    live = SimpleNamespace(id=uuid4())
    with patch("app.services.jd.jd_calibration_service.JDCalibrationService", return_value=calibration), \
            patch(f"{EFFECTS}.live_campaigns_for_jd", side_effect=lambda db, jd_id: [live] if jd_id == jd_a else []), \
            patch(f"{EFFECTS}.mark_campaigns_out_of_date", return_value=6) as mark:
        results = apply_jd_skill_change_effects(
            MagicMock(), [jd_a, jd_b, jd_a], actor_id="hr-1", actor_role="HR_ADMIN",
            note="unknown skill 'x' dropped", audit_service=MagicMock(), skill_repository=skill_repository)

    assert [c.args[0] for c in skill_repository.mark_jd_verified_if_fully_resolved.call_args_list] == [jd_a, jd_b]
    assert results == {jd_a: {"calibration_version": 5, "candidates_marked_out_of_date": 6},
                       jd_b: {"calibration_version": None, "candidates_marked_out_of_date": 0}}
    assert mark.call_count == 1 and mark.call_args.args[1] == [live.id]
    assert "unknown skill 'x' dropped" in mark.call_args.args[2]
