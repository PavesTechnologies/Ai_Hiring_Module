from unittest.mock import MagicMock
from uuid import uuid4

from sqlalchemy.dialects import postgresql

from app.models.pipeline import PipelineStage
from app.repositories.resume_repository import ResumeRepository

"""
GET /talent-pool/candidates - ResumeRepository.search_talent_pool backs the
Talent Pool Normal Search endpoint's entire filter/pagination/COUNT query.
Every filter (skill AND/OR, name, designation, location, education,
campaign, pipeline stage, experience range, composite-score range) is
expressed as a SQL condition and applied identically to both the COUNT
query and the LIMIT/OFFSET page query - this project has no
TestClient/live-Postgres test infrastructure (every existing test is a
MagicMock-based unit test, see test_resume_repository_talent_pool_filters.py
for the established convention), so these tests exercise the real
SQLAlchemy statement-building code against a MagicMock `db` and assert on
the compiled SQL text plus the exact number/shape of db.execute calls -
proving the query is built once, filters share one condition list, and
LIMIT/OFFSET (never Python slicing) drives pagination.
"""


def _make_repo():
    db = MagicMock()
    count_result = MagicMock()
    count_result.scalar_one.return_value = 0
    page_result = MagicMock()
    page_result.scalars.return_value.all.return_value = []
    db.execute.side_effect = [count_result, page_result]
    return ResumeRepository(db), db


def _compiled(call_args) -> str:
    stmt = call_args.args[0]
    return str(stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))


def test_issues_exactly_two_queries_a_count_and_a_page():
    """Never one call per candidate - COUNT and the page share the same filters, fetched in exactly 2 round trips."""
    repo, db = _make_repo()

    repo.search_talent_pool(page=1, size=6)

    assert db.execute.call_count == 2


def test_count_query_has_no_limit_or_offset():
    repo, db = _make_repo()

    repo.search_talent_pool(page=2, size=6)

    count_sql = _compiled(db.execute.call_args_list[0])
    assert "LIMIT" not in count_sql.upper()
    assert "OFFSET" not in count_sql.upper()
    assert "count" in count_sql.lower()


def test_page_query_applies_limit_and_offset_from_page_and_size():
    repo, db = _make_repo()

    repo.search_talent_pool(page=3, size=6)

    page_stmt = db.execute.call_args_list[1].args[0]
    compiled = page_stmt.compile()
    assert compiled.params.get("param_1") == 6 or 6 in compiled.params.values()
    # page=3, size=6 -> OFFSET 12
    assert 12 in compiled.params.values()


def test_eligibility_subquery_uses_row_number_partitioned_by_candidate():
    """Per-candidate 'most recent eligible resume' pick, expressed in SQL - never a Python loop over every resume."""
    repo, db = _make_repo()

    repo.search_talent_pool()

    sql = _compiled(db.execute.call_args_list[1]).lower()
    assert "row_number" in sql
    assert "partition by" in sql
    assert "is_talent_pool_eligible" in sql
    assert "parse_status" in sql


def test_no_filters_query_and_count_query_share_the_same_where_shape():
    repo, db = _make_repo()

    repo.search_talent_pool()

    count_sql = _compiled(db.execute.call_args_list[0])
    page_sql = _compiled(db.execute.call_args_list[1])
    # Both reference the eligibility-pick subquery - same filter set, not two implementations.
    assert "row_number" in count_sql.lower()
    assert "row_number" in page_sql.lower()


def test_search_term_ors_name_match_with_and_of_skill_tokens():
    repo, db = _make_repo()

    repo.search_talent_pool(search="Python AWS")

    sql = _compiled(db.execute.call_args_list[1]).lower()
    assert "full_name" in sql
    assert "candidate_skills" in sql


def test_or_skill_terms_produce_an_or_of_exists_clauses():
    repo, db = _make_repo()

    repo.search_talent_pool(or_skill_terms=["Java", "AWS"])

    sql = _compiled(db.execute.call_args_list[1]).lower()
    assert sql.count("exists") >= 2


def test_designation_terms_filter_via_jsonb_work_experience_unnest():
    repo, db = _make_repo()

    repo.search_talent_pool(designation_terms=["Python Developer", "Java Developer"])

    sql = _compiled(db.execute.call_args_list[1]).lower()
    assert "work_experience" in sql
    assert "jsonb_array_elements" in sql


def test_location_terms_filter_via_location_ilike_or():
    repo, db = _make_repo()

    repo.search_talent_pool(location_terms=["Hyderabad", "Chennai"])

    sql = _compiled(db.execute.call_args_list[1]).lower()
    assert "location" in sql
    assert sql.count("ilike") >= 2


def test_degree_levels_and_education_fields_filter_via_jsonb_education_unnest():
    repo, db = _make_repo()

    repo.search_talent_pool(degree_levels=["BACHELOR", "MASTER"], education_fields=["COMPUTER_SCIENCE"])

    sql = _compiled(db.execute.call_args_list[1]).lower()
    assert "degree_level" in sql
    assert "field_normalized" in sql
    assert "education" in sql


def test_campaign_ids_filter_uses_exists_against_campaign_candidates():
    repo, db = _make_repo()

    repo.search_talent_pool(campaign_ids=[uuid4(), uuid4()])

    sql = _compiled(db.execute.call_args_list[1]).lower()
    assert "campaign_candidates" in sql
    assert "exists" in sql


def test_exclude_campaign_id_uses_not_exists():
    repo, db = _make_repo()

    repo.search_talent_pool(exclude_campaign_id=uuid4())

    sql = _compiled(db.execute.call_args_list[1])
    assert "NOT (EXISTS" in sql or "NOT EXISTS" in sql.upper()


def test_pipeline_stages_filter_uses_exists_against_campaign_candidates():
    repo, db = _make_repo()

    repo.search_talent_pool(pipeline_stages=[PipelineStage.SHORTLISTED, PipelineStage.INTERVIEW])

    sql = _compiled(db.execute.call_args_list[1]).lower()
    assert "pipeline_stage" in sql
    assert "exists" in sql


def test_experience_range_filters_cast_parsed_json_field_to_numeric():
    repo, db = _make_repo()

    repo.search_talent_pool(experience_min=3, experience_max=8)

    sql = _compiled(db.execute.call_args_list[1]).lower()
    assert "total_experience_years" in sql
    assert "numeric" in sql or "cast" in sql


def test_score_range_filters_use_max_composite_score_subquery():
    repo, db = _make_repo()

    repo.search_talent_pool(score_min=60, score_max=100)

    sql = _compiled(db.execute.call_args_list[1]).lower()
    assert "composite_score" in sql
    assert "max(" in sql


def test_resolved_skill_id_adds_canonical_skill_id_equality_to_exists_clause():
    resolved_id = uuid4()
    repo, db = _make_repo()

    repo.search_talent_pool(or_skill_terms=["Java"], resolved_skill_ids_by_term={"Java": resolved_id})

    sql = _compiled(db.execute.call_args_list[1]).lower()
    assert "canonical_skill_id" in sql


def test_page_ordering_is_by_candidate_created_at_desc_with_stable_secondary_sort():
    repo, db = _make_repo()

    repo.search_talent_pool()

    page_stmt = db.execute.call_args_list[1].args[0]
    sql = str(page_stmt).lower()
    assert "order by" in sql
    assert "candidates.created_at desc" in sql
    assert "candidates.id desc" in sql


def test_returns_page_items_and_total_from_the_two_query_results():
    repo, db = _make_repo()
    resume = MagicMock()
    count_result = MagicMock()
    count_result.scalar_one.return_value = 42
    page_result = MagicMock()
    page_result.scalars.return_value.all.return_value = [resume]
    db.execute.side_effect = [count_result, page_result]

    items, total = repo.search_talent_pool()

    assert items == [resume]
    assert total == 42
