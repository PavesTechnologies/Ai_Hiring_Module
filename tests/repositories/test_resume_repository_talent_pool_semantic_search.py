from unittest.mock import MagicMock
from uuid import uuid4

from sqlalchemy.dialects import postgresql

from app.repositories.resume_repository import ResumeRepository

"""
M14 - POST /talent-pool/semantic-search backing query.
ResumeRepository.semantic_search_talent_pool must apply the exact same
structured-filter conditions Normal Search uses (_talent_pool_filter_conditions,
shared verbatim) BEFORE ranking by pgvector cosine distance - never rank the
whole Talent Pool and filter afterward. Mirrors
test_resume_repository_talent_pool_search.py's MagicMock + compiled-SQL
convention (this project has no TestClient/live-Postgres test
infrastructure).
"""


def _make_repo():
    db = MagicMock()
    count_result = MagicMock()
    count_result.scalar_one.return_value = 0
    page_result = MagicMock()
    page_result.all.return_value = []
    db.execute.side_effect = [count_result, page_result]
    return ResumeRepository(db), db


def _compiled(call_args) -> str:
    stmt = call_args.args[0]
    return str(stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))


def test_issues_exactly_two_queries_a_count_and_a_ranked_page():
    """Never one similarity computation per candidate in Python - COUNT and the ranked page share one filter set, in 2 round trips."""
    repo, db = _make_repo()

    repo.semantic_search_talent_pool(
        query_embedding=[0.1] * 384, embedding_model_version_id=uuid4(), page=1, size=6,
    )

    assert db.execute.call_count == 2


def test_count_query_has_no_order_by_limit_or_offset():
    repo, db = _make_repo()

    repo.semantic_search_talent_pool(
        query_embedding=[0.1] * 384, embedding_model_version_id=uuid4(), page=2, size=6,
    )

    count_sql = _compiled(db.execute.call_args_list[0])
    assert "LIMIT" not in count_sql.upper()
    assert "OFFSET" not in count_sql.upper()
    assert "<=>" not in count_sql
    assert "count" in count_sql.lower()


def test_ranked_page_query_is_ordered_by_cosine_distance_ascending():
    repo, db = _make_repo()

    repo.semantic_search_talent_pool(
        query_embedding=[0.1] * 384, embedding_model_version_id=uuid4(), page=1, size=6,
    )

    page_sql = _compiled(db.execute.call_args_list[1])
    assert "ORDER BY" in page_sql.upper()
    assert "<=>" in page_sql
    assert "LIMIT 6" in page_sql.upper()


def test_pagination_uses_sql_limit_offset_not_python_slicing():
    repo, db = _make_repo()

    repo.semantic_search_talent_pool(
        query_embedding=[0.1] * 384, embedding_model_version_id=uuid4(), page=3, size=6,
    )

    page_sql = _compiled(db.execute.call_args_list[1])
    assert "LIMIT 6" in page_sql.upper()
    assert "OFFSET 12" in page_sql.upper()


def test_ranked_page_query_is_scoped_to_the_active_embedding_model_version():
    repo, db = _make_repo()
    model_version_id = uuid4()

    repo.semantic_search_talent_pool(
        query_embedding=[0.1] * 384, embedding_model_version_id=model_version_id, page=1, size=6,
    )

    page_sql = _compiled(db.execute.call_args_list[1])
    assert str(model_version_id) in page_sql


def test_filter_conditions_appear_in_both_count_and_ranked_page_query():
    """FILTER FIRST - structured filters must narrow both the COUNT and the ranked-page query identically."""
    repo, db = _make_repo()

    repo.semantic_search_talent_pool(
        query_embedding=[0.1] * 384,
        embedding_model_version_id=uuid4(),
        location_terms=["Hyderabad", "Chennai"],
        experience_min=5,
        experience_max=10,
        page=1,
        size=6,
    )

    count_sql = _compiled(db.execute.call_args_list[0])
    page_sql = _compiled(db.execute.call_args_list[1])
    for sql in (count_sql, page_sql):
        assert "location" in sql.lower()
        assert "total_experience_years" in sql.lower()


def test_no_filters_still_scopes_to_the_eligible_resume_set_only():
    """Section 19 - no-filter case must still be exactly the eligible/one-resume-per-candidate set, no extra joins."""
    repo, db = _make_repo()

    repo.semantic_search_talent_pool(
        query_embedding=[0.1] * 384, embedding_model_version_id=uuid4(), page=1, size=6,
    )

    page_sql = _compiled(db.execute.call_args_list[1])
    assert "row_number" in page_sql.lower()
    assert "is_talent_pool_eligible" in page_sql.lower()


def test_query_never_matches_against_name_or_skill_tokens():
    """Section 23 - semantic search must never reuse Normal Search's name/skill `search` token matching."""
    repo, db = _make_repo()

    repo.semantic_search_talent_pool(
        query_embedding=[0.1] * 384, embedding_model_version_id=uuid4(), page=1, size=6,
    )

    page_sql = _compiled(db.execute.call_args_list[1])
    assert "full_name" not in page_sql.lower()
    assert "candidate_skills" not in page_sql.lower()


def test_architectural_proof_filters_are_applied_before_similarity_ranking():
    """
    Section 30 (mandatory) - FILTER FIRST, SEMANTIC SECOND. The ranked page
    is one single SQL statement, not two sequential steps: its WHERE clause
    (the structured filters + eligibility) textually precedes its ORDER BY
    (cosine distance) in the compiled statement, and both live in the exact
    same SELECT/FROM - Postgres therefore evaluates every filter predicate
    before it ever has a row to rank. A candidate failing the WHERE (e.g. a
    non-matching location) can never reach the ORDER BY at all, regardless
    of how similar their embedding is - there is no separate ranking step a
    filtered-out row could still appear in.
    """
    repo, db = _make_repo()

    repo.semantic_search_talent_pool(
        query_embedding=[0.1] * 384,
        embedding_model_version_id=uuid4(),
        location_terms=["Hyderabad"],
        page=1,
        size=6,
    )

    page_sql = _compiled(db.execute.call_args_list[1])
    location_index = page_sql.lower().index("location")
    # rindex, not index: the eligibility subquery has its own internal
    # "ORDER BY ... created_at DESC" (the ROW_NUMBER window) which appears
    # earlier in the statement - the OUTER ORDER BY that actually ranks the
    # page by cosine similarity is the last one, right before LIMIT/OFFSET.
    order_by_index = page_sql.upper().rindex("ORDER BY")
    limit_index = page_sql.upper().rindex("LIMIT")
    # The location filter sits inside the SELECT's WHERE clause, strictly
    # before the (outer) ORDER BY that ranks by cosine distance, which
    # itself sits strictly before the LIMIT/OFFSET pagination - one
    # statement, one ordered pipeline, filters always ahead of ranking.
    assert location_index < order_by_index < limit_index
    assert "<=>" in page_sql[order_by_index:limit_index]
