"""
Focused coverage for the M08 JD re-embedding trigger added to
SkillCurationService: whenever jd_skills are inserted (retroactive
unknown-skill resolution) or updated (HR remap) for an already-active JD,
that JD's embedding must be regenerated (force_regenerate=True) via
EmbeddingQueueService.queue_jd_embedding. Not a full test of
SkillCurationService's existing unknown-skill-resolution behavior (no
prior test file covers that at all) - just the new trigger.
"""
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

from app.models.skills import JDSkillVerificationStatus, UnknownSkillStatus
from app.services.embedding_queue_service import JDEmbeddingQueueError
from app.services.skills.skill_curation_service import SkillCurationService


def _make_unknown_skill(raw_text="Golang"):
    return SimpleNamespace(id=uuid4(), raw_text=raw_text, status=UnknownSkillStatus.PENDING)


def _make_jd_link(jd_id=None, mandatory=True):
    return SimpleNamespace(jd_id=jd_id or uuid4(), mandatory=mandatory)


def _make_service():
    skill_repository = MagicMock()
    audit_service = MagicMock()
    embedding_queue_service = MagicMock()
    encryption_service = MagicMock()
    resume_repository = MagicMock()
    reevaluation_queue_service = MagicMock()

    service = SkillCurationService(
        skill_repository=skill_repository,
        audit_service=audit_service,
        embedding_queue_service=embedding_queue_service,
        encryption_service=encryption_service,
        resume_repository=resume_repository,
        reevaluation_queue_service=reevaluation_queue_service,
    )
    return service, skill_repository, embedding_queue_service


# ----------------------------------------------------------------------
# _create_retroactive_jd_skills - returns only the jd_ids that actually
# got a NEW JDSkill row, never ones that already had an independently-
# matched row for this canonical skill (jd_skills genuinely unchanged there).
# ----------------------------------------------------------------------

def test_create_retroactive_jd_skills_returns_only_jds_with_a_new_insert():
    service, skill_repository, _ = _make_service()
    unknown_skill = _make_unknown_skill()
    link_needs_insert = _make_jd_link()
    link_already_has_skill = _make_jd_link()
    skill_repository.get_pending_jd_links.return_value = [link_needs_insert, link_already_has_skill]
    canonical_skill_id = uuid4()

    def _get_jd_skill(jd_id, skill_id):
        return None if jd_id == link_needs_insert.jd_id else SimpleNamespace()

    skill_repository.get_jd_skill.side_effect = _get_jd_skill

    touched = service._create_retroactive_jd_skills(unknown_skill, canonical_skill_id)

    assert touched == {link_needs_insert.jd_id}
    skill_repository.create_jd_skill.assert_called_once()
    # Both links still get marked resolved regardless of whether an insert happened.
    assert skill_repository.mark_jd_unknown_skill_resolved.call_count == 2


def test_create_retroactive_jd_skills_returns_empty_set_when_nothing_new():
    service, skill_repository, _ = _make_service()
    unknown_skill = _make_unknown_skill()
    link = _make_jd_link()
    skill_repository.get_pending_jd_links.return_value = [link]
    skill_repository.get_jd_skill.return_value = SimpleNamespace()  # already exists

    touched = service._create_retroactive_jd_skills(unknown_skill, uuid4())

    assert touched == set()
    skill_repository.create_jd_skill.assert_not_called()


# ----------------------------------------------------------------------
# map_to_existing_skill / promote_to_canonical - both call
# _create_retroactive_jd_skills directly and must trigger re-embedding
# for exactly the jd_ids that changed, only after commit.
# ----------------------------------------------------------------------

def test_map_to_existing_skill_triggers_reembedding_for_touched_jds_after_commit():
    service, skill_repository, embedding_queue_service = _make_service()
    unknown_skill = _make_unknown_skill()
    target_skill = SimpleNamespace(id=uuid4(), canonical_name="Go")
    skill_repository.get_unknown_skill_by_id.return_value = unknown_skill
    skill_repository.get_skill_by_id.return_value = target_skill
    link = _make_jd_link()
    skill_repository.get_pending_jd_links.return_value = [link]
    skill_repository.get_jd_skill.return_value = None

    commit_order = []
    skill_repository.commit.side_effect = lambda: commit_order.append("commit")
    embedding_queue_service.queue_jd_embedding.side_effect = lambda *a, **k: commit_order.append("reembed")

    service.map_to_existing_skill(unknown_skill.id, target_skill.id, actor_id="hr_user")

    embedding_queue_service.queue_jd_embedding.assert_called_once_with(link.jd_id, force_regenerate=True)
    assert commit_order == ["commit", "reembed"]


def test_map_to_existing_skill_does_not_trigger_reembedding_when_no_jd_skills_changed():
    service, skill_repository, embedding_queue_service = _make_service()
    unknown_skill = _make_unknown_skill()
    target_skill = SimpleNamespace(id=uuid4(), canonical_name="Go")
    skill_repository.get_unknown_skill_by_id.return_value = unknown_skill
    skill_repository.get_skill_by_id.return_value = target_skill
    skill_repository.get_pending_jd_links.return_value = []

    service.map_to_existing_skill(unknown_skill.id, target_skill.id, actor_id="hr_user")

    embedding_queue_service.queue_jd_embedding.assert_not_called()


def test_promote_to_canonical_triggers_reembedding_for_touched_jds():
    service, skill_repository, embedding_queue_service = _make_service()
    unknown_skill = _make_unknown_skill(raw_text="Rust")
    skill_repository.get_unknown_skill_by_id.return_value = unknown_skill
    skill_repository.find_skill_by_name_or_alias.return_value = None
    new_skill = SimpleNamespace(id=uuid4(), canonical_name="Rust")
    skill_repository.create_skill_ontology.return_value = new_skill
    link = _make_jd_link()
    skill_repository.get_pending_jd_links.return_value = [link]
    skill_repository.get_jd_skill.return_value = None

    service.promote_to_canonical(unknown_skill.id, actor_id="hr_user")

    embedding_queue_service.queue_jd_embedding.assert_called_once_with(link.jd_id, force_regenerate=True)


# ----------------------------------------------------------------------
# resolve_unknown_skill - reuses _create_retroactive_jd_skills too.
# ----------------------------------------------------------------------

def test_resolve_unknown_skill_triggers_reembedding_for_touched_jds():
    from app.schemas.unknown_skill.skill_resolution_request import UnknownSkillResolutionType

    service, skill_repository, embedding_queue_service = _make_service()
    unknown_skill = _make_unknown_skill(raw_text="Kotlin")
    target_skill = SimpleNamespace(id=uuid4(), canonical_name="Kotlin")
    skill_repository.get_unknown_skill_by_id.return_value = unknown_skill
    skill_repository.get_skill_by_id.return_value = target_skill
    link = _make_jd_link()
    skill_repository.get_pending_jd_links.return_value = [link]
    skill_repository.get_jd_skill.return_value = None
    skill_repository.get_candidate_skills_by_unknown_skill_id.return_value = []

    service.resolve_unknown_skill(
        unknown_skill.id, target_skill.id, UnknownSkillResolutionType.MAP_TO_EXISTING, actor_id="hr_user",
    )

    embedding_queue_service.queue_jd_embedding.assert_called_once_with(link.jd_id, force_regenerate=True)


# ----------------------------------------------------------------------
# create_canonical_skill_from_unknown / bulk_approve_unknown_skills - go
# through _finalize_unknown_skill_resolution, which now threads
# affected_jd_ids the same way it already threads affected_resume_ids.
# ----------------------------------------------------------------------

def test_create_canonical_skill_from_unknown_triggers_reembedding_for_touched_jds():
    service, skill_repository, embedding_queue_service = _make_service()
    unknown_skill = _make_unknown_skill(raw_text="Elixir")
    skill_repository.get_unknown_skill_by_id.return_value = unknown_skill
    skill_repository.find_skill_by_name_or_alias.return_value = None
    new_skill = SimpleNamespace(id=uuid4(), canonical_name="Elixir")
    skill_repository.create_skill_ontology.return_value = new_skill
    link = _make_jd_link()
    skill_repository.get_pending_jd_links.return_value = [link]
    skill_repository.get_jd_skill.return_value = None
    skill_repository.get_candidate_skills_by_unknown_skill_id.return_value = []

    service.create_canonical_skill_from_unknown(unknown_skill.id, actor_id="hr_user", canonical_name="Elixir")

    embedding_queue_service.queue_jd_embedding.assert_called_once_with(link.jd_id, force_regenerate=True)


def test_bulk_approve_triggers_reembedding_independently_per_item():
    service, skill_repository, embedding_queue_service = _make_service()
    unknown_skill_a = _make_unknown_skill(raw_text="Zig")
    unknown_skill_b = _make_unknown_skill(raw_text="Nim")
    link_a = _make_jd_link()
    link_b = _make_jd_link()

    def _get_unknown_skill(uid):
        return unknown_skill_a if uid == unknown_skill_a.id else unknown_skill_b

    def _get_pending_jd_links(uid):
        return [link_a] if uid == unknown_skill_a.id else [link_b]

    skill_repository.get_unknown_skill_by_id.side_effect = _get_unknown_skill
    skill_repository.find_skill_by_name_or_alias.return_value = None
    skill_repository.create_skill_ontology.side_effect = lambda **kwargs: SimpleNamespace(
        id=uuid4(), canonical_name=kwargs["canonical_name"],
    )
    skill_repository.get_pending_jd_links.side_effect = _get_pending_jd_links
    skill_repository.get_jd_skill.return_value = None
    skill_repository.get_candidate_skills_by_unknown_skill_id.return_value = []

    service.bulk_approve_unknown_skills([unknown_skill_a.id, unknown_skill_b.id], actor_id="hr_user")

    assert embedding_queue_service.queue_jd_embedding.call_count == 2
    called_jd_ids = {
        call.args[0] for call in embedding_queue_service.queue_jd_embedding.call_args_list
    }
    assert called_jd_ids == {link_a.jd_id, link_b.jd_id}


def test_delete_unknown_skill_never_triggers_reembedding():
    """Pure delete never creates jd_skills - jd_skills are untouched, so no re-embed trigger."""
    service, skill_repository, embedding_queue_service = _make_service()
    unknown_skill = _make_unknown_skill(raw_text="Junk")
    skill_repository.get_unknown_skill_by_id.return_value = unknown_skill
    skill_repository.get_jd_links_by_unknown_skill_id.return_value = []
    skill_repository.delete_unknown_skill_cascade.return_value = {
        "jd_unknown_skills_deleted": 0, "candidate_skills_deleted": 0,
    }

    service.delete_unknown_skill(unknown_skill.id, actor_id="hr_user")

    embedding_queue_service.queue_jd_embedding.assert_not_called()


# ----------------------------------------------------------------------
# remap_jd_skill - HR overriding an existing JDSkill's canonical mapping
# in place is itself a jd_skills UPDATE.
# ----------------------------------------------------------------------

def test_remap_jd_skill_triggers_reembedding_after_commit():
    service, skill_repository, embedding_queue_service = _make_service()
    jd_skill = SimpleNamespace(id=uuid4(), jd_id=uuid4(), canonical_skill_id=uuid4())
    skill_repository.get_jd_skill_by_id.return_value = jd_skill
    new_skill = SimpleNamespace(id=uuid4(), canonical_name="TypeScript")
    skill_repository.get_skill_by_id.return_value = new_skill

    commit_order = []
    skill_repository.commit.side_effect = lambda: commit_order.append("commit")
    embedding_queue_service.queue_jd_embedding.side_effect = lambda *a, **k: commit_order.append("reembed")

    service.remap_jd_skill(jd_skill.id, new_skill.id, actor_id="hr_user")

    embedding_queue_service.queue_jd_embedding.assert_called_once_with(jd_skill.jd_id, force_regenerate=True)
    assert commit_order == ["commit", "reembed"]


# ----------------------------------------------------------------------
# _trigger_jd_reembedding - fire-and-forget, must never crash the caller.
# ----------------------------------------------------------------------

def test_trigger_jd_reembedding_swallows_queue_error():
    service, _, embedding_queue_service = _make_service()
    embedding_queue_service.queue_jd_embedding.side_effect = JDEmbeddingQueueError(
        "broker unreachable", jd_id=uuid4(), task_id=uuid4(),
    )

    # Must not raise.
    service._trigger_jd_reembedding({uuid4()})


def test_trigger_jd_reembedding_noop_on_empty_set():
    service, _, embedding_queue_service = _make_service()

    service._trigger_jd_reembedding(set())

    embedding_queue_service.queue_jd_embedding.assert_not_called()
