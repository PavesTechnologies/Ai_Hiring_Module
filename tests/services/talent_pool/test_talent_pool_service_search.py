from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

from app.services.talent_pool.talent_pool_service import TalentPoolService

"""
M13-E01 S02 - Talent Pool Search and Skill-Based Filtering.

search_candidates never selects a resume - it reuses
ResumeSelectionService._is_eligible (mocked here, exactly like every other
TalentPoolService test mocks resume_selection_service entirely) so these
tests only verify TalentPoolService's own orchestration: skill resolution,
eligibility filtering, dedup-per-candidate, pagination, ordering. Eligibility
predicate correctness itself is covered by test_resume_selection_service.py.
"""


def _make_candidate(candidate_id=None, created_at=None):
    return SimpleNamespace(
        id=candidate_id or uuid4(),
        full_name_encrypted=b"encrypted-name",
        email_encrypted=b"encrypted-email",
        encryption_key_id=uuid4(),
        jurisdiction="GLOBAL",
        created_at=created_at or datetime.now(timezone.utc),
    )


def _make_resume(candidate_id, resume_id=None, version_number=1, created_at=None, parsed_json=None):
    return SimpleNamespace(
        id=resume_id or uuid4(),
        candidate_id=candidate_id,
        version_number=version_number,
        parsed_json=parsed_json,
        created_at=created_at or datetime.now(timezone.utc),
    )


def make_service(
    candidate_repo=None,
    resume_repo=None,
    campaign_candidate_repo=None,
    resume_selection_service=None,
    skill_repo=None,
    encryption_service=None,
):
    encryption_service = encryption_service or MagicMock()
    encryption_service.decrypt.side_effect = lambda ciphertext, key_id: "Jane Doe" if ciphertext == b"encrypted-name" else "jane@example.com"

    # Card-enrichment batch lookups default to "nothing found" (empty dict)
    # unless the caller already configured .return_value (before calling
    # make_service) - without this default, a bare MagicMock() would flow
    # into TalentPoolSearchItem's skills/best_composite_score fields and
    # fail Pydantic validation on every test that doesn't care about
    # enrichment.
    resume_repo = resume_repo or MagicMock()
    if not isinstance(resume_repo.get_canonical_skills_by_resume_ids.return_value, dict):
        resume_repo.get_canonical_skills_by_resume_ids.return_value = {}
    campaign_candidate_repo = campaign_candidate_repo or MagicMock()
    if not isinstance(campaign_candidate_repo.get_best_composite_scores_by_candidate_ids.return_value, dict):
        campaign_candidate_repo.get_best_composite_scores_by_candidate_ids.return_value = {}

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
        skill_repo=skill_repo or MagicMock(),
    )


def test_search_without_skill_uses_all_parsed_resumes():
    resume_repo = MagicMock()
    resume_repo.get_all_parsed.return_value = []
    candidate_repo = MagicMock()
    candidate_repo.get_by_ids.return_value = []
    service = make_service(resume_repo=resume_repo, candidate_repo=candidate_repo)

    result = service.search_candidates()

    resume_repo.get_all_parsed.assert_called_once()
    resume_repo.get_by_skill_match.assert_not_called()
    assert result.items == []
    assert result.total == 0


def test_search_with_skill_resolves_canonical_skill_and_queries_by_it():
    skill_repo = MagicMock()
    resolved = SimpleNamespace(id=uuid4())
    skill_repo.find_skill_by_name_or_alias.return_value = resolved
    resume_repo = MagicMock()
    resume_repo.get_by_skill_match.return_value = []
    candidate_repo = MagicMock()
    candidate_repo.get_by_ids.return_value = []
    service = make_service(resume_repo=resume_repo, skill_repo=skill_repo, candidate_repo=candidate_repo)

    service.search_candidates(skill="Java")

    skill_repo.find_skill_by_name_or_alias.assert_called_once_with("Java")
    call_kwargs = resume_repo.get_by_skill_match.call_args.kwargs
    assert call_kwargs["canonical_skill_id"] == resolved.id
    assert call_kwargs["raw_text_pattern"] == "%Java%"


def test_search_with_unresolved_skill_still_queries_by_raw_text_only():
    skill_repo = MagicMock()
    skill_repo.find_skill_by_name_or_alias.return_value = None
    resume_repo = MagicMock()
    resume_repo.get_by_skill_match.return_value = []
    candidate_repo = MagicMock()
    candidate_repo.get_by_ids.return_value = []
    service = make_service(resume_repo=resume_repo, skill_repo=skill_repo, candidate_repo=candidate_repo)

    service.search_candidates(skill="SomeUncatalogedSkill")

    call_kwargs = resume_repo.get_by_skill_match.call_args.kwargs
    assert call_kwargs["canonical_skill_id"] is None
    assert call_kwargs["raw_text_pattern"] == "%SomeUncatalogedSkill%"


def test_search_escapes_like_wildcards_in_skill_query():
    resume_repo = MagicMock()
    resume_repo.get_by_skill_match.return_value = []
    candidate_repo = MagicMock()
    candidate_repo.get_by_ids.return_value = []
    skill_repo = MagicMock()
    skill_repo.find_skill_by_name_or_alias.return_value = None
    service = make_service(resume_repo=resume_repo, skill_repo=skill_repo, candidate_repo=candidate_repo)

    service.search_candidates(skill="100%_java")

    call_kwargs = resume_repo.get_by_skill_match.call_args.kwargs
    assert call_kwargs["raw_text_pattern"] == "%100\\%\\_java%"


def test_search_excludes_candidates_with_no_eligible_resume():
    candidate_id = uuid4()
    resume = _make_resume(candidate_id)
    resume_repo = MagicMock()
    resume_repo.get_all_parsed.return_value = [resume]
    resume_selection_service = MagicMock()
    resume_selection_service._is_eligible.return_value = False
    candidate_repo = MagicMock()
    candidate_repo.get_by_ids.return_value = []
    service = make_service(
        resume_repo=resume_repo, resume_selection_service=resume_selection_service, candidate_repo=candidate_repo,
    )

    result = service.search_candidates()

    assert result.total == 0
    candidate_repo.get_by_ids.assert_called_once_with([])


def test_search_includes_candidates_with_an_eligible_resume():
    candidate = _make_candidate()
    resume = _make_resume(candidate.id, version_number=3)
    resume_repo = MagicMock()
    resume_repo.get_all_parsed.return_value = [resume]
    resume_selection_service = MagicMock()
    resume_selection_service._is_eligible.return_value = True
    candidate_repo = MagicMock()
    candidate_repo.get_by_ids.return_value = [candidate]
    service = make_service(
        resume_repo=resume_repo, resume_selection_service=resume_selection_service, candidate_repo=candidate_repo,
    )

    result = service.search_candidates()

    assert result.total == 1
    assert len(result.items) == 1
    item = result.items[0]
    assert item.candidate.candidate_id == candidate.id
    assert item.matching_resume_id == resume.id
    assert item.matching_resume_version == 3


def test_search_dedupes_to_one_matching_resume_per_candidate():
    """A candidate matched via more than one eligible resume version still appears once."""
    candidate = _make_candidate()
    newer_resume = _make_resume(candidate.id, version_number=2)
    older_resume = _make_resume(candidate.id, version_number=1)
    resume_repo = MagicMock()
    # get_by_skill_match/get_all_parsed already order most-recent-first.
    resume_repo.get_all_parsed.return_value = [newer_resume, older_resume]
    resume_selection_service = MagicMock()
    resume_selection_service._is_eligible.return_value = True
    candidate_repo = MagicMock()
    candidate_repo.get_by_ids.return_value = [candidate]
    service = make_service(
        resume_repo=resume_repo, resume_selection_service=resume_selection_service, candidate_repo=candidate_repo,
    )

    result = service.search_candidates()

    assert result.total == 1
    assert result.items[0].matching_resume_id == newer_resume.id


def test_search_never_calls_select_resume_for_campaign():
    """Read-only per spec - resume selection must only happen on add-to-campaign."""
    candidate = _make_candidate()
    resume = _make_resume(candidate.id)
    resume_repo = MagicMock()
    resume_repo.get_all_parsed.return_value = [resume]
    resume_selection_service = MagicMock()
    resume_selection_service._is_eligible.return_value = True
    candidate_repo = MagicMock()
    candidate_repo.get_by_ids.return_value = [candidate]
    service = make_service(
        resume_repo=resume_repo, resume_selection_service=resume_selection_service, candidate_repo=candidate_repo,
    )

    service.search_candidates()

    resume_selection_service.select_resume_for_campaign.assert_not_called()


def test_search_paginates_results():
    candidates = [_make_candidate(created_at=datetime(2026, 1, i + 1, tzinfo=timezone.utc)) for i in range(5)]
    resumes = [_make_resume(candidate.id) for candidate in candidates]
    resume_repo = MagicMock()
    resume_repo.get_all_parsed.return_value = resumes
    resume_selection_service = MagicMock()
    resume_selection_service._is_eligible.return_value = True
    candidate_repo = MagicMock()
    # Returns a fresh copy each call - the service sorts the returned list
    # in place, which would otherwise mutate this test's own `candidates`
    # reference (MagicMock returns the exact object passed to return_value).
    candidate_repo.get_by_ids.side_effect = lambda ids: list(candidates)
    service = make_service(
        resume_repo=resume_repo, resume_selection_service=resume_selection_service, candidate_repo=candidate_repo,
    )

    result = service.search_candidates(page=1, size=2)

    assert result.total == 5
    assert len(result.items) == 2
    # Most-recently-created candidate first.
    assert result.items[0].candidate.candidate_id == candidates[-1].id
    assert result.items[1].candidate.candidate_id == candidates[-2].id


def test_search_second_page_returns_remaining_results():
    candidates = [_make_candidate(created_at=datetime(2026, 1, i + 1, tzinfo=timezone.utc)) for i in range(5)]
    resumes = [_make_resume(candidate.id) for candidate in candidates]
    resume_repo = MagicMock()
    resume_repo.get_all_parsed.return_value = resumes
    resume_selection_service = MagicMock()
    resume_selection_service._is_eligible.return_value = True
    candidate_repo = MagicMock()
    # Returns a fresh copy each call - the service sorts the returned list
    # in place, which would otherwise mutate this test's own `candidates`
    # reference (MagicMock returns the exact object passed to return_value).
    candidate_repo.get_by_ids.side_effect = lambda ids: list(candidates)
    service = make_service(
        resume_repo=resume_repo, resume_selection_service=resume_selection_service, candidate_repo=candidate_repo,
    )

    result = service.search_candidates(page=3, size=2)

    assert result.total == 5
    assert len(result.items) == 1
    assert result.items[0].candidate.candidate_id == candidates[0].id


"""
Card enrichment (M13-E01 S02 T0x) - summary/skills/best_composite_score.
summary is read straight off the matching resume's own parsed_json (no
extra query); skills and best_composite_score come from the two batched
repository lookups mocked here exactly like every other repo dependency in
this file - these tests verify TalentPoolService's own wiring (which
resume/candidate each field is read from, that per-candidate data never
cross-contaminates, and that the batch lookups are called once per search
regardless of page size), not the repositories' SQL - that's covered by
test_resume_repository_canonical_skills.py and
test_campaign_candidate_best_composite_scores.py.
"""


def test_search_includes_summary_from_matching_resume_parsed_json():
    candidate = _make_candidate()
    resume = _make_resume(candidate.id, parsed_json={"summary": "Senior backend engineer."})
    resume_repo = MagicMock()
    resume_repo.get_all_parsed.return_value = [resume]
    resume_selection_service = MagicMock()
    resume_selection_service._is_eligible.return_value = True
    candidate_repo = MagicMock()
    candidate_repo.get_by_ids.return_value = [candidate]
    service = make_service(
        resume_repo=resume_repo, resume_selection_service=resume_selection_service, candidate_repo=candidate_repo,
    )

    result = service.search_candidates()

    assert result.items[0].summary == "Senior backend engineer."


def test_search_returns_none_summary_when_resume_has_no_parsed_json():
    candidate = _make_candidate()
    resume = _make_resume(candidate.id, parsed_json=None)
    resume_repo = MagicMock()
    resume_repo.get_all_parsed.return_value = [resume]
    resume_selection_service = MagicMock()
    resume_selection_service._is_eligible.return_value = True
    candidate_repo = MagicMock()
    candidate_repo.get_by_ids.return_value = [candidate]
    service = make_service(
        resume_repo=resume_repo, resume_selection_service=resume_selection_service, candidate_repo=candidate_repo,
    )

    result = service.search_candidates()

    assert result.items[0].summary is None


def test_search_returns_none_summary_when_parsed_json_has_no_summary_key():
    candidate = _make_candidate()
    resume = _make_resume(candidate.id, parsed_json={"total_experience_years": 5})
    resume_repo = MagicMock()
    resume_repo.get_all_parsed.return_value = [resume]
    resume_selection_service = MagicMock()
    resume_selection_service._is_eligible.return_value = True
    candidate_repo = MagicMock()
    candidate_repo.get_by_ids.return_value = [candidate]
    service = make_service(
        resume_repo=resume_repo, resume_selection_service=resume_selection_service, candidate_repo=candidate_repo,
    )

    result = service.search_candidates()

    assert result.items[0].summary is None


def test_search_includes_multiple_skills_from_batched_lookup():
    candidate = _make_candidate()
    resume = _make_resume(candidate.id)
    resume_repo = MagicMock()
    resume_repo.get_all_parsed.return_value = [resume]
    resume_repo.get_canonical_skills_by_resume_ids.return_value = {
        resume.id: ["Java", "Spring Boot", "REST API", "Docker", "AWS"],
    }
    resume_selection_service = MagicMock()
    resume_selection_service._is_eligible.return_value = True
    candidate_repo = MagicMock()
    candidate_repo.get_by_ids.return_value = [candidate]
    service = make_service(
        resume_repo=resume_repo, resume_selection_service=resume_selection_service, candidate_repo=candidate_repo,
    )

    result = service.search_candidates()

    assert result.items[0].skills == ["Java", "Spring Boot", "REST API", "Docker", "AWS"]
    resume_repo.get_canonical_skills_by_resume_ids.assert_called_once_with([resume.id])


def test_search_returns_empty_skills_list_when_candidate_has_no_skills():
    candidate = _make_candidate()
    resume = _make_resume(candidate.id)
    resume_repo = MagicMock()
    resume_repo.get_all_parsed.return_value = [resume]
    resume_repo.get_canonical_skills_by_resume_ids.return_value = {}
    resume_selection_service = MagicMock()
    resume_selection_service._is_eligible.return_value = True
    candidate_repo = MagicMock()
    candidate_repo.get_by_ids.return_value = [candidate]
    service = make_service(
        resume_repo=resume_repo, resume_selection_service=resume_selection_service, candidate_repo=candidate_repo,
    )

    result = service.search_candidates()

    assert result.items[0].skills == []


def test_search_uses_best_composite_score_from_batched_lookup():
    """MAX-across-campaigns is computed by the repository (unit-tested separately);
    this only verifies the service routes the right value to the right candidate."""
    candidate = _make_candidate()
    resume = _make_resume(candidate.id)
    resume_repo = MagicMock()
    resume_repo.get_all_parsed.return_value = [resume]
    campaign_candidate_repo = MagicMock()
    campaign_candidate_repo.get_best_composite_scores_by_candidate_ids.return_value = {candidate.id: 92.5}
    resume_selection_service = MagicMock()
    resume_selection_service._is_eligible.return_value = True
    candidate_repo = MagicMock()
    candidate_repo.get_by_ids.return_value = [candidate]
    service = make_service(
        resume_repo=resume_repo,
        campaign_candidate_repo=campaign_candidate_repo,
        resume_selection_service=resume_selection_service,
        candidate_repo=candidate_repo,
    )

    result = service.search_candidates()

    assert result.items[0].best_composite_score == 92.5
    campaign_candidate_repo.get_best_composite_scores_by_candidate_ids.assert_called_once_with([candidate.id])


def test_search_returns_none_best_composite_score_when_no_campaign_score():
    candidate = _make_candidate()
    resume = _make_resume(candidate.id)
    resume_repo = MagicMock()
    resume_repo.get_all_parsed.return_value = [resume]
    campaign_candidate_repo = MagicMock()
    # Candidate absent from the batch result entirely - no scored campaign_candidates row.
    campaign_candidate_repo.get_best_composite_scores_by_candidate_ids.return_value = {}
    resume_selection_service = MagicMock()
    resume_selection_service._is_eligible.return_value = True
    candidate_repo = MagicMock()
    candidate_repo.get_by_ids.return_value = [candidate]
    service = make_service(
        resume_repo=resume_repo,
        campaign_candidate_repo=campaign_candidate_repo,
        resume_selection_service=resume_selection_service,
        candidate_repo=candidate_repo,
    )

    result = service.search_candidates()

    assert result.items[0].best_composite_score is None


def test_search_returns_independent_enrichment_data_for_multiple_candidates():
    """Each candidate's summary/skills/score must never leak onto another candidate's item."""
    candidate_a = _make_candidate(created_at=datetime(2026, 1, 2, tzinfo=timezone.utc))
    candidate_b = _make_candidate(created_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
    resume_a = _make_resume(candidate_a.id, parsed_json={"summary": "Frontend engineer."})
    resume_b = _make_resume(candidate_b.id, parsed_json={"summary": "Data scientist."})
    resume_repo = MagicMock()
    resume_repo.get_all_parsed.return_value = [resume_a, resume_b]
    resume_repo.get_canonical_skills_by_resume_ids.return_value = {
        resume_a.id: ["React", "TypeScript"],
        resume_b.id: ["Python", "Pandas"],
    }
    campaign_candidate_repo = MagicMock()
    campaign_candidate_repo.get_best_composite_scores_by_candidate_ids.return_value = {
        candidate_a.id: 88.0,
        candidate_b.id: 74.5,
    }
    resume_selection_service = MagicMock()
    resume_selection_service._is_eligible.return_value = True
    candidate_repo = MagicMock()
    candidate_repo.get_by_ids.return_value = [candidate_a, candidate_b]
    service = make_service(
        resume_repo=resume_repo,
        campaign_candidate_repo=campaign_candidate_repo,
        resume_selection_service=resume_selection_service,
        candidate_repo=candidate_repo,
    )

    result = service.search_candidates()

    by_candidate_id = {item.candidate.candidate_id: item for item in result.items}
    item_a = by_candidate_id[candidate_a.id]
    item_b = by_candidate_id[candidate_b.id]

    assert item_a.summary == "Frontend engineer."
    assert item_a.skills == ["React", "TypeScript"]
    assert item_a.best_composite_score == 88.0

    assert item_b.summary == "Data scientist."
    assert item_b.skills == ["Python", "Pandas"]
    assert item_b.best_composite_score == 74.5


def test_search_pagination_still_works_with_enrichment_wired_in():
    candidates = [_make_candidate(created_at=datetime(2026, 1, i + 1, tzinfo=timezone.utc)) for i in range(5)]
    resumes = [_make_resume(candidate.id) for candidate in candidates]
    resume_repo = MagicMock()
    resume_repo.get_all_parsed.return_value = resumes
    resume_selection_service = MagicMock()
    resume_selection_service._is_eligible.return_value = True
    candidate_repo = MagicMock()
    candidate_repo.get_by_ids.side_effect = lambda ids: list(candidates)
    service = make_service(
        resume_repo=resume_repo, resume_selection_service=resume_selection_service, candidate_repo=candidate_repo,
    )

    result = service.search_candidates(page=2, size=2)

    assert result.total == 5
    assert result.page == 2
    assert result.size == 2
    assert len(result.items) == 2
    assert result.items[0].candidate.candidate_id == candidates[2].id
    assert result.items[1].candidate.candidate_id == candidates[1].id


def test_search_enrichment_does_not_introduce_n_plus_one_queries():
    """Regardless of how many candidates are on the page, each batch lookup fires exactly once."""
    candidates = [_make_candidate(created_at=datetime(2026, 1, i + 1, tzinfo=timezone.utc)) for i in range(5)]
    resumes = [_make_resume(candidate.id) for candidate in candidates]
    resume_repo = MagicMock()
    resume_repo.get_all_parsed.return_value = resumes
    campaign_candidate_repo = MagicMock()
    campaign_candidate_repo.get_best_composite_scores_by_candidate_ids.return_value = {}
    resume_selection_service = MagicMock()
    resume_selection_service._is_eligible.return_value = True
    candidate_repo = MagicMock()
    candidate_repo.get_by_ids.side_effect = lambda ids: list(candidates)
    service = make_service(
        resume_repo=resume_repo,
        campaign_candidate_repo=campaign_candidate_repo,
        resume_selection_service=resume_selection_service,
        candidate_repo=candidate_repo,
    )

    service.search_candidates(page=1, size=5)

    # One batched call for all 5 candidates' skills, one for all 5 candidates'
    # scores - never one call per candidate.
    resume_repo.get_canonical_skills_by_resume_ids.assert_called_once()
    campaign_candidate_repo.get_best_composite_scores_by_candidate_ids.assert_called_once()
    assert len(resume_repo.get_canonical_skills_by_resume_ids.call_args.args[0]) == 5
    assert len(campaign_candidate_repo.get_best_composite_scores_by_candidate_ids.call_args.args[0]) == 5


"""
M13-E01 S02 T0z - multiple skills (OR'd together) + designation substring
filter, both added to search_candidates on top of the existing single-skill
search — `skill` (singular) is kept working unchanged for backward
compatibility.
"""


def test_search_with_multiple_skills_ors_results_together():
    candidate_a = _make_candidate()
    candidate_b = _make_candidate()
    resume_a = _make_resume(candidate_a.id)
    resume_b = _make_resume(candidate_b.id)

    def by_skill_match(canonical_skill_id, raw_text_pattern):
        if raw_text_pattern == "%Java%":
            return [resume_a]
        if raw_text_pattern == "%AWS%":
            return [resume_b]
        return []

    resume_repo = MagicMock()
    resume_repo.get_by_skill_match.side_effect = by_skill_match
    skill_repo = MagicMock()
    skill_repo.find_skill_by_name_or_alias.return_value = None
    resume_selection_service = MagicMock()
    resume_selection_service._is_eligible.return_value = True
    candidate_repo = MagicMock()
    candidate_repo.get_by_ids.return_value = [candidate_a, candidate_b]
    service = make_service(
        resume_repo=resume_repo, skill_repo=skill_repo,
        resume_selection_service=resume_selection_service, candidate_repo=candidate_repo,
    )

    result = service.search_candidates(skills=["Java", "AWS"])

    assert result.total == 2
    assert resume_repo.get_by_skill_match.call_count == 2
    candidate_ids = {item.candidate.candidate_id for item in result.items}
    assert candidate_ids == {candidate_a.id, candidate_b.id}


def test_search_multiple_skills_dedupes_resume_matched_by_more_than_one_term():
    candidate = _make_candidate()
    resume = _make_resume(candidate.id)
    resume_repo = MagicMock()
    # Same resume matches both terms.
    resume_repo.get_by_skill_match.return_value = [resume]
    skill_repo = MagicMock()
    skill_repo.find_skill_by_name_or_alias.return_value = None
    resume_selection_service = MagicMock()
    resume_selection_service._is_eligible.return_value = True
    candidate_repo = MagicMock()
    candidate_repo.get_by_ids.return_value = [candidate]
    service = make_service(
        resume_repo=resume_repo, skill_repo=skill_repo,
        resume_selection_service=resume_selection_service, candidate_repo=candidate_repo,
    )

    result = service.search_candidates(skills=["Java", "Spring Boot"])

    assert result.total == 1
    # Deduped by resume.id before eligibility is ever evaluated - not
    # checked twice for the same resume.
    assert resume_selection_service._is_eligible.call_count == 1


def test_search_singular_skill_param_still_works_alongside_skills_list():
    """Backward compatibility: `skill` folds into the same OR'd term list as `skills`."""
    candidate = _make_candidate()
    resume = _make_resume(candidate.id)
    resume_repo = MagicMock()
    resume_repo.get_by_skill_match.return_value = [resume]
    skill_repo = MagicMock()
    skill_repo.find_skill_by_name_or_alias.return_value = None
    resume_selection_service = MagicMock()
    resume_selection_service._is_eligible.return_value = True
    candidate_repo = MagicMock()
    candidate_repo.get_by_ids.return_value = [candidate]
    service = make_service(
        resume_repo=resume_repo, skill_repo=skill_repo,
        resume_selection_service=resume_selection_service, candidate_repo=candidate_repo,
    )

    result = service.search_candidates(skill="Java", skills=["AWS"])

    assert result.total == 1
    assert resume_repo.get_by_skill_match.call_count == 2


def test_search_filters_by_designation_substring_case_insensitive():
    candidate_a = _make_candidate()
    candidate_b = _make_candidate()
    resume_a = _make_resume(candidate_a.id, parsed_json={
        "work_experience": [{"title": "Senior Backend Engineer", "is_current": True}],
    })
    resume_b = _make_resume(candidate_b.id, parsed_json={
        "work_experience": [{"title": "Product Manager", "is_current": True}],
    })
    resume_repo = MagicMock()
    resume_repo.get_all_parsed.return_value = [resume_a, resume_b]
    resume_selection_service = MagicMock()
    resume_selection_service._is_eligible.return_value = True
    candidate_repo = MagicMock()
    candidate_repo.get_by_ids.side_effect = lambda ids: [c for c in [candidate_a, candidate_b] if c.id in ids]
    service = make_service(
        resume_repo=resume_repo, resume_selection_service=resume_selection_service, candidate_repo=candidate_repo,
    )

    result = service.search_candidates(designation="engineer")

    assert result.total == 1
    assert result.items[0].candidate.candidate_id == candidate_a.id
    assert result.items[0].candidate.designation == "Senior Backend Engineer"


def test_search_designation_filter_excludes_candidates_with_no_designation():
    candidate = _make_candidate()
    resume = _make_resume(candidate.id, parsed_json={"work_experience": []})
    resume_repo = MagicMock()
    resume_repo.get_all_parsed.return_value = [resume]
    resume_selection_service = MagicMock()
    resume_selection_service._is_eligible.return_value = True
    candidate_repo = MagicMock()
    # Must respect the (now-filtered, empty) id list passed in, like a real
    # repository would - a static return_value here would mask the
    # designation filter ever having removed this candidate.
    candidate_repo.get_by_ids.side_effect = lambda ids: [c for c in [candidate] if c.id in ids]
    service = make_service(
        resume_repo=resume_repo, resume_selection_service=resume_selection_service, candidate_repo=candidate_repo,
    )

    result = service.search_candidates(designation="engineer")

    assert result.total == 0


def test_search_combines_skill_and_designation_filters():
    candidate_a = _make_candidate()
    candidate_b = _make_candidate()
    resume_a = _make_resume(candidate_a.id, parsed_json={
        "work_experience": [{"title": "Backend Engineer", "is_current": True}],
    })
    resume_b = _make_resume(candidate_b.id, parsed_json={
        "work_experience": [{"title": "QA Engineer", "is_current": True}],
    })
    resume_repo = MagicMock()
    resume_repo.get_by_skill_match.return_value = [resume_a, resume_b]
    skill_repo = MagicMock()
    skill_repo.find_skill_by_name_or_alias.return_value = None
    resume_selection_service = MagicMock()
    resume_selection_service._is_eligible.return_value = True
    candidate_repo = MagicMock()
    candidate_repo.get_by_ids.side_effect = lambda ids: [c for c in [candidate_a, candidate_b] if c.id in ids]
    service = make_service(
        resume_repo=resume_repo, skill_repo=skill_repo,
        resume_selection_service=resume_selection_service, candidate_repo=candidate_repo,
    )

    result = service.search_candidates(skills=["Java"], designation="Backend")

    assert result.total == 1
    assert result.items[0].candidate.candidate_id == candidate_a.id


"""
campaign_id exclusion filter - "who's left to add" view when browsing the
Talent Pool for one specific campaign. Purely a candidate_id exclusion over
the already-eligible set; never touches resume selection.
"""


def test_search_without_campaign_id_never_queries_campaign_membership():
    candidate = _make_candidate()
    resume = _make_resume(candidate.id)
    resume_repo = MagicMock()
    resume_repo.get_all_parsed.return_value = [resume]
    resume_selection_service = MagicMock()
    resume_selection_service._is_eligible.return_value = True
    candidate_repo = MagicMock()
    candidate_repo.get_by_ids.return_value = [candidate]
    campaign_candidate_repo = MagicMock()
    service = make_service(
        resume_repo=resume_repo, resume_selection_service=resume_selection_service,
        candidate_repo=candidate_repo, campaign_candidate_repo=campaign_candidate_repo,
    )

    result = service.search_candidates()

    assert result.total == 1
    campaign_candidate_repo.get_candidate_ids_by_campaign.assert_not_called()


def test_search_excludes_candidates_already_in_the_given_campaign():
    campaign_id = uuid4()
    candidate_already_in = _make_candidate()
    candidate_not_yet_added = _make_candidate()
    resume_already_in = _make_resume(candidate_already_in.id)
    resume_not_yet_added = _make_resume(candidate_not_yet_added.id)
    resume_repo = MagicMock()
    resume_repo.get_all_parsed.return_value = [resume_already_in, resume_not_yet_added]
    resume_selection_service = MagicMock()
    resume_selection_service._is_eligible.return_value = True
    campaign_candidate_repo = MagicMock()
    campaign_candidate_repo.get_candidate_ids_by_campaign.return_value = {candidate_already_in.id}
    candidate_repo = MagicMock()
    candidate_repo.get_by_ids.side_effect = (
        lambda ids: [c for c in [candidate_already_in, candidate_not_yet_added] if c.id in ids]
    )
    service = make_service(
        resume_repo=resume_repo, resume_selection_service=resume_selection_service,
        candidate_repo=candidate_repo, campaign_candidate_repo=campaign_candidate_repo,
    )

    result = service.search_candidates(campaign_id=campaign_id)

    campaign_candidate_repo.get_candidate_ids_by_campaign.assert_called_once_with(campaign_id)
    assert result.total == 1
    assert result.items[0].candidate.candidate_id == candidate_not_yet_added.id


def test_search_combines_campaign_id_exclusion_with_skill_filter():
    campaign_id = uuid4()
    candidate_already_in = _make_candidate()
    candidate_not_yet_added = _make_candidate()
    resume_already_in = _make_resume(candidate_already_in.id)
    resume_not_yet_added = _make_resume(candidate_not_yet_added.id)
    resume_repo = MagicMock()
    resume_repo.get_by_skill_match.return_value = [resume_already_in, resume_not_yet_added]
    skill_repo = MagicMock()
    skill_repo.find_skill_by_name_or_alias.return_value = None
    resume_selection_service = MagicMock()
    resume_selection_service._is_eligible.return_value = True
    campaign_candidate_repo = MagicMock()
    campaign_candidate_repo.get_candidate_ids_by_campaign.return_value = {candidate_already_in.id}
    candidate_repo = MagicMock()
    candidate_repo.get_by_ids.side_effect = (
        lambda ids: [c for c in [candidate_already_in, candidate_not_yet_added] if c.id in ids]
    )
    service = make_service(
        resume_repo=resume_repo, skill_repo=skill_repo, resume_selection_service=resume_selection_service,
        candidate_repo=candidate_repo, campaign_candidate_repo=campaign_candidate_repo,
    )

    result = service.search_candidates(skills=["Java"], campaign_id=campaign_id)

    assert result.total == 1
    assert result.items[0].candidate.candidate_id == candidate_not_yet_added.id
