from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

from app.services.talent_pool.talent_pool_service import TALENT_POOL_MAX_PAGE_SIZE, TalentPoolService

"""
M13-E01 S02 — Talent Pool Normal Search and filters.

search_candidates no longer filters/paginates in Python at all — every
filter (skill AND/OR, name, designation, location, education, campaign,
pipeline stage, experience range, composite-score range) plus COUNT and
LIMIT/OFFSET pagination is delegated to
ResumeRepository.search_talent_pool, which is unit-tested on its own SQL
merits in test_resume_repository_talent_pool_search.py. These tests only
verify TalentPoolService's own orchestration: skill-term resolution, the
resolved_skill_ids_by_term dict passed to the repository, the freshness
config lookup, page-size capping, and the batched candidate/enrichment
lookups feeding the response - never that any filtering happens here.
"""


def _make_candidate(candidate_id=None):
    return SimpleNamespace(
        id=candidate_id or uuid4(),
        full_name_encrypted=b"encrypted-name",
        email_encrypted=b"encrypted-email",
        encryption_key_id=uuid4(),
        jurisdiction="GLOBAL",
        created_at=datetime.now(timezone.utc),
    )


def _make_resume(candidate_id, resume_id=None, version_number=1, parsed_json=None):
    return SimpleNamespace(
        id=resume_id or uuid4(),
        candidate_id=candidate_id,
        version_number=version_number,
        parsed_json=parsed_json,
        created_at=datetime.now(timezone.utc),
    )


def make_service(
    candidate_repo=None,
    resume_repo=None,
    campaign_candidate_repo=None,
    resume_selection_service=None,
    skill_repo=None,
    config_repo=None,
    encryption_service=None,
):
    encryption_service = encryption_service or MagicMock()
    encryption_service.decrypt.side_effect = (
        lambda ciphertext, key_id: "Jane Doe" if ciphertext == b"encrypted-name" else "jane@example.com"
    )

    resume_repo = resume_repo or MagicMock()
    if not isinstance(resume_repo.search_talent_pool.return_value, tuple):
        resume_repo.search_talent_pool.return_value = ([], 0)
    if not isinstance(resume_repo.get_canonical_skills_by_resume_ids.return_value, dict):
        resume_repo.get_canonical_skills_by_resume_ids.return_value = {}

    campaign_candidate_repo = campaign_candidate_repo or MagicMock()
    if not isinstance(campaign_candidate_repo.get_best_composite_scores_by_candidate_ids.return_value, dict):
        campaign_candidate_repo.get_best_composite_scores_by_candidate_ids.return_value = {}

    skill_repo = skill_repo or MagicMock()
    if skill_repo.find_skill_by_name_or_alias.side_effect is None and \
            not isinstance(skill_repo.find_skill_by_name_or_alias.return_value, SimpleNamespace):
        skill_repo.find_skill_by_name_or_alias.return_value = None

    return TalentPoolService(
        candidate_repo=candidate_repo or MagicMock(),
        resume_repo=resume_repo,
        campaign_repo=MagicMock(),
        campaign_candidate_repo=campaign_candidate_repo,
        consent_repo=MagicMock(),
        encryption_service=encryption_service,
        audit_service=MagicMock(),
        celery_task_log_service=MagicMock(),
        resume_selection_service=resume_selection_service or MagicMock(),
        skill_repo=skill_repo,
        config_repo=config_repo,
    )


def test_search_never_loads_all_candidates_into_python():
    """No Python-side filtering path exists — get_all_parsed/get_by_skill_match are gone."""
    resume_repo = MagicMock()
    candidate_repo = MagicMock()
    candidate_repo.get_by_ids.return_value = []
    service = make_service(resume_repo=resume_repo, candidate_repo=candidate_repo)

    service.search_candidates()

    resume_repo.search_talent_pool.assert_called_once()
    assert not hasattr(resume_repo, "get_all_parsed") or not resume_repo.get_all_parsed.called
    assert not resume_repo.get_by_skill_match.called


def test_search_with_no_results_returns_empty_response():
    resume_repo = MagicMock()
    resume_repo.search_talent_pool.return_value = ([], 0)
    candidate_repo = MagicMock()
    candidate_repo.get_by_ids.return_value = []
    service = make_service(resume_repo=resume_repo, candidate_repo=candidate_repo)

    result = service.search_candidates()

    assert result.items == []
    assert result.total == 0


def test_search_builds_item_from_repository_page():
    candidate = _make_candidate()
    resume = _make_resume(candidate.id, version_number=3, parsed_json={"summary": "Senior engineer."})
    resume_repo = MagicMock()
    resume_repo.search_talent_pool.return_value = ([resume], 1)
    resume_repo.get_canonical_skills_by_resume_ids.return_value = {resume.id: ["Java"]}
    candidate_repo = MagicMock()
    candidate_repo.get_by_ids.return_value = [candidate]
    campaign_candidate_repo = MagicMock()
    campaign_candidate_repo.get_best_composite_scores_by_candidate_ids.return_value = {candidate.id: 91.2}
    service = make_service(
        resume_repo=resume_repo, candidate_repo=candidate_repo, campaign_candidate_repo=campaign_candidate_repo,
    )

    result = service.search_candidates()

    assert result.total == 1
    item = result.items[0]
    assert item.candidate.candidate_id == candidate.id
    assert item.matching_resume_id == resume.id
    assert item.matching_resume_version == 3
    assert item.summary == "Senior engineer."
    assert item.skills == ["Java"]
    assert item.best_composite_score == 91.2


def test_search_batches_enrichment_lookups_regardless_of_page_size():
    candidates = [_make_candidate() for _ in range(3)]
    resumes = [_make_resume(c.id) for c in candidates]
    resume_repo = MagicMock()
    resume_repo.search_talent_pool.return_value = (resumes, 3)
    candidate_repo = MagicMock()
    candidate_repo.get_by_ids.return_value = candidates
    campaign_candidate_repo = MagicMock()
    service = make_service(
        resume_repo=resume_repo, candidate_repo=candidate_repo, campaign_candidate_repo=campaign_candidate_repo,
    )

    service.search_candidates()

    resume_repo.get_canonical_skills_by_resume_ids.assert_called_once()
    campaign_candidate_repo.get_best_composite_scores_by_candidate_ids.assert_called_once()
    assert len(resume_repo.get_canonical_skills_by_resume_ids.call_args.args[0]) == 3
    assert len(campaign_candidate_repo.get_best_composite_scores_by_candidate_ids.call_args.args[0]) == 3


"""Page-size capping — the endpoint's hard limit of TALENT_POOL_MAX_PAGE_SIZE."""


def test_default_size_is_capped_at_max_page_size():
    resume_repo = MagicMock()
    candidate_repo = MagicMock()
    candidate_repo.get_by_ids.return_value = []
    service = make_service(resume_repo=resume_repo, candidate_repo=candidate_repo)

    service.search_candidates(size=12)

    call_kwargs = resume_repo.search_talent_pool.call_args.kwargs
    assert call_kwargs["size"] == TALENT_POOL_MAX_PAGE_SIZE


def test_oversized_size_request_is_capped():
    resume_repo = MagicMock()
    candidate_repo = MagicMock()
    candidate_repo.get_by_ids.return_value = []
    service = make_service(resume_repo=resume_repo, candidate_repo=candidate_repo)

    result = service.search_candidates(size=100)

    assert resume_repo.search_talent_pool.call_args.kwargs["size"] == TALENT_POOL_MAX_PAGE_SIZE
    assert result.size == TALENT_POOL_MAX_PAGE_SIZE


def test_size_within_cap_is_passed_through_unchanged():
    resume_repo = MagicMock()
    candidate_repo = MagicMock()
    candidate_repo.get_by_ids.return_value = []
    service = make_service(resume_repo=resume_repo, candidate_repo=candidate_repo)

    result = service.search_candidates(size=3)

    assert resume_repo.search_talent_pool.call_args.kwargs["size"] == 3
    assert result.size == 3


"""Skill-term resolution — the only Python-side work search_candidates still
does before delegating to the repository: resolving each distinct skill-like
term (from `search`'s AND tokens and the legacy OR'd skill/skills params) to
its canonical SkillOntology id via the small skill_ontology table."""


def test_search_resolves_each_search_token_to_a_canonical_skill_id():
    skill_repo = MagicMock()
    resolved = SimpleNamespace(id=uuid4())
    skill_repo.find_skill_by_name_or_alias.side_effect = lambda term: resolved if term == "Python" else None
    resume_repo = MagicMock()
    candidate_repo = MagicMock()
    candidate_repo.get_by_ids.return_value = []
    service = make_service(resume_repo=resume_repo, skill_repo=skill_repo, candidate_repo=candidate_repo)

    service.search_candidates(search="Python AWS")

    call_kwargs = resume_repo.search_talent_pool.call_args.kwargs
    assert call_kwargs["resolved_skill_ids_by_term"] == {"Python": resolved.id, "AWS": None}
    assert call_kwargs["search"] == "Python AWS"


def test_search_folds_singular_skill_into_or_skill_terms():
    resume_repo = MagicMock()
    candidate_repo = MagicMock()
    candidate_repo.get_by_ids.return_value = []
    skill_repo = MagicMock()
    skill_repo.find_skill_by_name_or_alias.return_value = None
    service = make_service(resume_repo=resume_repo, skill_repo=skill_repo, candidate_repo=candidate_repo)

    service.search_candidates(skill="Java", skills=["AWS"])

    call_kwargs = resume_repo.search_talent_pool.call_args.kwargs
    assert call_kwargs["or_skill_terms"] == ["AWS", "Java"]


def test_search_folds_singular_designation_and_location_into_lists():
    resume_repo = MagicMock()
    candidate_repo = MagicMock()
    candidate_repo.get_by_ids.return_value = []
    service = make_service(resume_repo=resume_repo, candidate_repo=candidate_repo)

    service.search_candidates(
        designation="Engineer", designations=["Manager"], location="Chennai", locations=["Hyderabad"],
    )

    call_kwargs = resume_repo.search_talent_pool.call_args.kwargs
    assert call_kwargs["designation_terms"] == ["Manager", "Engineer"]
    assert call_kwargs["location_terms"] == ["Hyderabad", "Chennai"]


def test_search_passes_every_new_filter_straight_through_to_the_repository():
    resume_repo = MagicMock()
    candidate_repo = MagicMock()
    candidate_repo.get_by_ids.return_value = []
    campaign_ids = [uuid4(), uuid4()]
    service = make_service(resume_repo=resume_repo, candidate_repo=candidate_repo)

    service.search_candidates(
        degree_levels=["BACHELOR"],
        education_fields=["COMPUTER_SCIENCE"],
        campaign_ids=campaign_ids,
        pipeline_stages=["SHORTLISTED"],
        experience_min=3,
        experience_max=8,
        score_min=60,
        score_max=100,
    )

    call_kwargs = resume_repo.search_talent_pool.call_args.kwargs
    assert call_kwargs["degree_levels"] == ["BACHELOR"]
    assert call_kwargs["education_fields"] == ["COMPUTER_SCIENCE"]
    assert call_kwargs["campaign_ids"] == campaign_ids
    assert call_kwargs["pipeline_stages"] == ["SHORTLISTED"]
    assert call_kwargs["experience_min"] == 3
    assert call_kwargs["experience_max"] == 8
    assert call_kwargs["score_min"] == 60
    assert call_kwargs["score_max"] == 100


def test_search_campaign_id_exclusion_is_independent_of_campaign_ids_inclusion():
    resume_repo = MagicMock()
    candidate_repo = MagicMock()
    candidate_repo.get_by_ids.return_value = []
    campaign_id = uuid4()
    service = make_service(resume_repo=resume_repo, candidate_repo=candidate_repo)

    service.search_candidates(campaign_id=campaign_id)

    call_kwargs = resume_repo.search_talent_pool.call_args.kwargs
    assert call_kwargs["exclude_campaign_id"] == campaign_id
    assert call_kwargs["campaign_ids"] is None


"""Freshness config — the one platform-config read the service performs
itself, mirroring ResumeSelectionService._is_fresh's own lookup so the SQL
eligibility window can never drift from add-to-campaign's freshness rule."""


def test_search_reads_freshness_config_and_passes_it_to_the_repository():
    resume_repo = MagicMock()
    candidate_repo = MagicMock()
    candidate_repo.get_by_ids.return_value = []
    config_repo = MagicMock()
    config_repo.get_configs_by_keys.return_value = {"RESUME_FRESHNESS_MAX_AGE_DAYS": "90"}
    service = make_service(resume_repo=resume_repo, candidate_repo=candidate_repo, config_repo=config_repo)

    service.search_candidates()

    config_repo.get_configs_by_keys.assert_called_once_with(["RESUME_FRESHNESS_MAX_AGE_DAYS"])
    assert resume_repo.search_talent_pool.call_args.kwargs["freshness_max_age_days"] == 90


def test_search_falls_back_to_default_freshness_when_config_repo_is_absent():
    resume_repo = MagicMock()
    candidate_repo = MagicMock()
    candidate_repo.get_by_ids.return_value = []
    service = make_service(resume_repo=resume_repo, candidate_repo=candidate_repo, config_repo=None)

    service.search_candidates()

    assert resume_repo.search_talent_pool.call_args.kwargs["freshness_max_age_days"] == 180


def test_search_never_calls_select_resume_for_campaign():
    """Read-only per spec - resume selection must only happen on add-to-campaign."""
    resume_repo = MagicMock()
    candidate_repo = MagicMock()
    candidate_repo.get_by_ids.return_value = []
    resume_selection_service = MagicMock()
    service = make_service(
        resume_repo=resume_repo, candidate_repo=candidate_repo, resume_selection_service=resume_selection_service,
    )

    service.search_candidates()

    resume_selection_service.select_resume_for_campaign.assert_not_called()
