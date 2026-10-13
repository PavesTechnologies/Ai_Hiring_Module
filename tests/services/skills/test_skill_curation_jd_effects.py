"""
Global unknown-skill curation (map, approve as new, dismiss, delete) gives
every JD it resolves the shared follow-up - verification recomputed, a
calibration version if calibrated, live candidates out of date - and records
the jd_skills rows it creates as verified by the HR user.
"""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from app.models.skills import JDSkillVerificationStatus, JDUnknownSkillResolution, UnknownSkillStatus
from app.services.skills.skill_curation_service import SkillCurationService

EFFECTS = "app.services.skills.skill_curation_service.apply_jd_skill_change_effects"


def _link(jd_id=None):
    return SimpleNamespace(jd_id=jd_id or uuid4(), mandatory=True, resolution=None, resolved_by=None,
                           resolved_at=None, resolved_canonical_skill_id=None)


def _service():
    skill_repository = MagicMock()
    skill_repository.get_jd_skill.return_value = None
    skill_repository.find_skill_by_name_or_alias.return_value = None
    skill_repository.get_candidate_skills_by_unknown_skill_id.return_value = []
    skill_repository.delete_unknown_skill_cascade.return_value = {"jd_unknown_skills_deleted": 0,
                                                                  "candidate_skills_deleted": 0}
    jd_repository = MagicMock()
    jd_repository.get_by_id.return_value = SimpleNamespace(extracted_json={})
    service = SkillCurationService(
        skill_repository=skill_repository, audit_service=MagicMock(), embedding_queue_service=MagicMock(),
        encryption_service=MagicMock(), resume_repository=MagicMock(), reevaluation_queue_service=MagicMock(),
        jd_repository=jd_repository,
    )
    return service, skill_repository


@pytest.fixture
def effects():
    with patch(EFFECTS, side_effect=lambda db, jd_ids, **kw: {j: {"calibration_version": None,
                                                                    "candidates_marked_out_of_date": 1}
                                                                for j in jd_ids}) as apply:
        yield apply


def test_approve_as_new_gives_every_waiting_jd_the_follow_up(effects):
    service, repo = _service()
    unknown = SimpleNamespace(id=uuid4(), raw_text="Agentic Workflows", status=UnknownSkillStatus.PENDING,
                              normalized_key=None)
    links = [_link(), _link()]
    repo.get_unknown_skill_by_id.return_value = unknown
    repo.get_pending_jd_links.return_value = links
    repo.create_skill_ontology.return_value = SimpleNamespace(id=uuid4(), canonical_name="Agentic Workflows")

    service.create_canonical_skill_from_unknown(unknown.id, "hr-1", canonical_name="Agentic Workflows")

    assert set(effects.call_args.args[1]) == {links[0].jd_id, links[1].jd_id}
    assert effects.call_args.kwargs["actor_id"] == "hr-1"
    assert "approved as new skill 'Agentic Workflows'" in effects.call_args.kwargs["note"]
    created = repo.create_jd_skill.call_args.kwargs
    assert created["verification_status"] == JDSkillVerificationStatus.HR_VERIFIED and created["verified_by"] == "hr-1"
    assert all(link.resolution == JDUnknownSkillResolution.NEW_SKILL for link in links)
    assert len(service.last_change_effects) == 2


def test_map_to_existing_gives_the_follow_up_too(effects):
    service, repo = _service()
    unknown = SimpleNamespace(id=uuid4(), raw_text="Prompt Design", status=UnknownSkillStatus.PENDING,
                              normalized_key=None)
    link = _link()
    repo.get_unknown_skill_by_id.return_value = unknown
    repo.get_skill_by_id.return_value = SimpleNamespace(id=uuid4(), canonical_name="Prompt Engineering")
    repo.get_pending_jd_links.return_value = [link]

    service.map_to_existing_skill(unknown.id, uuid4(), "hr-1")

    assert effects.call_args.args[1] == {link.jd_id}
    assert link.resolution == JDUnknownSkillResolution.MAPPED and link.resolved_by == "hr-1"


def test_dismiss_drops_the_skill_from_every_waiting_jd(effects):
    service, repo = _service()
    unknown = SimpleNamespace(id=uuid4(), raw_text="Synergy", status=UnknownSkillStatus.PENDING)
    links = [_link(), _link()]
    repo.get_unknown_skill_by_id.return_value = unknown
    repo.get_pending_jd_links.return_value = links

    service.dismiss(unknown.id, "hr-1")

    assert all(link.resolution == JDUnknownSkillResolution.DROPPED and link.resolved_by == "hr-1" for link in links)
    assert repo.mark_jd_unknown_skill_resolved.call_count == 2
    assert effects.call_args.args[1] == {links[0].jd_id, links[1].jd_id}
    repo.create_jd_skill.assert_not_called()
    repo.commit.assert_called_once()


def test_deleting_an_unknown_skill_updates_the_jds_that_were_waiting_on_it(effects):
    service, repo = _service()
    unknown = SimpleNamespace(id=uuid4(), raw_text="Junk", status=UnknownSkillStatus.PENDING)
    link = _link()
    repo.get_unknown_skill_by_id.return_value = unknown
    repo.get_pending_jd_links.return_value = [link]
    repo.get_jd_links_by_unknown_skill_id.return_value = [(link, SimpleNamespace(id=link.jd_id))]

    service.delete_unknown_skill(unknown.id, "hr-1")

    assert effects.call_args.args[1] == {link.jd_id}
    assert "deleted" in effects.call_args.kwargs["note"]


def test_nothing_waiting_means_no_follow_up(effects):
    service, repo = _service()
    unknown = SimpleNamespace(id=uuid4(), raw_text="Synergy", status=UnknownSkillStatus.PENDING)
    repo.get_unknown_skill_by_id.return_value = unknown
    repo.get_pending_jd_links.return_value = []

    service.dismiss(unknown.id, "hr-1")

    effects.assert_not_called()
