from decimal import Decimal
from unittest.mock import MagicMock
from uuid import uuid4

from app.models.jd.job_descriptions import JDSourceFormat
from app.schemas.ai.jd_extraction_response import JDExtractionResponse
from app.services.jd.jd_service import JDService, _DEFAULT_JD_SKILL_WEIGHT
from app.services.skills.skill_normalization_service import SkillMatchResult, SkillMatchTier


def _match(raw_text, mandatory, canonical_skill_id=None):
    return SkillMatchResult(
        raw_text=raw_text,
        mandatory=mandatory,
        canonical_skill_id=canonical_skill_id or uuid4(),
        match_tier=SkillMatchTier.EXACT,
        confidence=1.0,
        normalized_text=raw_text,
    )


def make_jd_service():
    repository = MagicMock()
    repository.get_by_content_hash.return_value = None
    repository.get_duplicate_excluding_lineage.return_value = None

    def _create_job_description(jd):
        jd.id = uuid4()  # SQLAlchemy's default=uuid4 only applies on a real flush
        return jd

    repository.create_job_description.side_effect = _create_job_description

    audit_service = MagicMock()
    service = JDService(
        repository=repository, hash_service=MagicMock(), audit_service=audit_service,
        storage_service=MagicMock(),
    )
    return service, repository


def _persist(service, skill_matches, **overrides):
    skill_repository = MagicMock()
    skill_repository.upsert_unknown_skill.return_value = (MagicMock(id=uuid4(), raw_text="x"), False)

    kwargs = dict(
        title="Backend Engineer",
        raw_text="raw jd text",
        jurisdiction="GLOBAL",
        min_experience_years=None,
        education_criteria=None,
        source_format=JDSourceFormat.PDF,
        file_path=None,
        created_by=str(uuid4()),
        content_hash=f"hash-{uuid4()}",
        extraction=JDExtractionResponse(required_skills=[], preferred_skills=[]),
        skill_repository=skill_repository,
        skill_matches=skill_matches,
        embedding=[0.0] * 384,
        embedding_model_version_id=uuid4(),
        input_text_hash="text-hash",
    )
    kwargs.update(overrides)
    jd_id = service.persist_processed_jd(**kwargs)
    return jd_id, skill_repository


def _weights_by_mandatory(skill_repository):
    mandatory_weights, preferred_weights = [], []
    for call in skill_repository.create_jd_skill.call_args_list:
        weight = call.kwargs["weight"]
        (mandatory_weights if call.kwargs["mandatory"] else preferred_weights).append(weight)
    return mandatory_weights, preferred_weights


# ---------------------------------------------------------------- real JD parsing pipeline


def test_persist_processed_jd_never_leaves_weight_null():
    service, _ = make_jd_service()
    matches = [_match(f"skill-{i}", mandatory=True) for i in range(4)] + [
        _match(f"pref-{i}", mandatory=False) for i in range(2)
    ]
    _, skill_repository = _persist(service, matches)

    for call in skill_repository.create_jd_skill.call_args_list:
        assert call.kwargs["weight"] is not None


def test_every_matched_skill_gets_the_same_flat_equal_weight():
    """
    No 100-point budget, no per-group split - every mandatory AND
    preferred skill gets the identical constant weight. deterministic_score
    is a ratio (actual/max), so the scale is self-normalizing regardless
    of this constant's magnitude.
    """
    service, _ = make_jd_service()
    matches = [_match(f"skill-{i}", mandatory=True) for i in range(4)] + [
        _match(f"pref-{i}", mandatory=False) for i in range(2)
    ]
    _, skill_repository = _persist(service, matches)

    mandatory_weights, preferred_weights = _weights_by_mandatory(skill_repository)
    assert len(mandatory_weights) == 4
    assert len(preferred_weights) == 2
    assert all(w == _DEFAULT_JD_SKILL_WEIGHT for w in mandatory_weights + preferred_weights)


def test_single_mandatory_skill_gets_the_default_weight():
    service, _ = make_jd_service()
    matches = [_match("only-skill", mandatory=True)]
    _, skill_repository = _persist(service, matches)

    mandatory_weights, preferred_weights = _weights_by_mandatory(skill_repository)
    assert mandatory_weights == [_DEFAULT_JD_SKILL_WEIGHT]
    assert preferred_weights == []


def test_three_mandatory_skills_all_get_the_same_weight():
    service, _ = make_jd_service()
    matches = [_match(f"skill-{i}", mandatory=True) for i in range(3)]
    _, skill_repository = _persist(service, matches)

    mandatory_weights, _ = _weights_by_mandatory(skill_repository)
    assert mandatory_weights == [_DEFAULT_JD_SKILL_WEIGHT] * 3


def test_uneven_eleven_mandatory_skills_all_get_the_same_weight():
    """No remainder/rounding concern at all now - it's a flat constant, not a divided budget."""
    service, _ = make_jd_service()
    matches = [_match(f"skill-{i}", mandatory=True) for i in range(11)]
    _, skill_repository = _persist(service, matches)

    mandatory_weights, _ = _weights_by_mandatory(skill_repository)
    assert mandatory_weights == [_DEFAULT_JD_SKILL_WEIGHT] * 11


def test_zero_mandatory_skills_only_preferred_group_gets_weights():
    service, _ = make_jd_service()
    matches = [_match(f"pref-{i}", mandatory=False) for i in range(2)]
    _, skill_repository = _persist(service, matches)

    mandatory_weights, preferred_weights = _weights_by_mandatory(skill_repository)
    assert mandatory_weights == []
    assert preferred_weights == [_DEFAULT_JD_SKILL_WEIGHT] * 2


def test_reprocessing_creates_fresh_jd_skill_rows_without_touching_previous_version():
    """
    Reprocessing (JDReprocessRequired flow) always creates a brand-new
    JobDescription row (new jd_id) rather than mutating the existing one -
    persist_processed_jd never calls any method that updates an existing
    jd_skills row's weight, so a prior version's (potentially manually
    configured, in a future feature) weights are structurally untouched.
    """
    service, repository = make_jd_service()
    existing_jd_id = uuid4()
    repository.has_active_campaign.return_value = False
    repository.get_by_id.return_value = MagicMock(id=existing_jd_id)

    matches = [_match("skill-a", mandatory=True)]
    new_jd_id, skill_repository = _persist(
        service, matches, existing_jd_id=existing_jd_id, version_number=2,
        parent_jd_id=existing_jd_id, lineage_root_id=existing_jd_id,
    )

    assert new_jd_id != existing_jd_id
    repository.deactivate_version.assert_called_once()
    for call in skill_repository.create_jd_skill.call_args_list:
        assert call.kwargs["jd_id"] == new_jd_id
        assert call.kwargs["jd_id"] != existing_jd_id
    # Nothing in this flow ever calls an "update jd_skill weight" method
    # against the old version - no such method is even invoked here.
    assert not hasattr(skill_repository, "update_jd_skill_weight") or not skill_repository.update_jd_skill_weight.called
