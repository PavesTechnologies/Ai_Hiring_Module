from unittest.mock import MagicMock
from uuid import uuid4

from app.models.pipeline import AIEvaluationStatus, AIRecommendation, PipelineStage
from app.repositories.campaign_candidate_repository import CampaignCandidateRepository


def _make_repo(execute_results=None):
    db = MagicMock()
    repo = CampaignCandidateRepository(db)
    if execute_results is not None:
        db.execute.side_effect = execute_results
    return repo, db


def _count_result(value):
    result = MagicMock()
    result.scalar.return_value = value
    return result


def _rows_result(rows):
    result = MagicMock()
    result.all.return_value = rows
    return result


# ----------------------------------------------------------------------
# get_ranked_by_campaign - SQL structure: ranking must be performed by
# PostgreSQL (ORDER BY/WHERE/LIMIT/OFFSET in the generated SQL), never in
# Python. We assert against the compiled statement text rather than mocking
# a real database, since no real-DB test infrastructure exists in this
# project (every other repository test in this codebase follows the same
# MagicMock-session convention).
# ----------------------------------------------------------------------

def test_default_ranking_orders_by_composite_desc_nulls_last_then_deterministic_then_tiebreakers():
    campaign_id = uuid4()
    repo, db = _make_repo([_count_result(0), _rows_result([])])

    repo.get_ranked_by_campaign(campaign_id, page=1, page_size=20)

    main_stmt = db.execute.call_args_list[1].args[0]
    sql = str(main_stmt)
    order_by_clause = sql.split("ORDER BY", 1)[1].split("\n LIMIT", 1)[0]
    assert "campaign_candidates.composite_score DESC NULLS LAST" in order_by_clause
    assert "campaign_candidates.deterministic_score DESC" in order_by_clause
    assert "campaign_candidates.created_at ASC" in order_by_clause
    assert order_by_clause.rstrip().endswith("campaign_candidates.id ASC")
    # Deterministic ranking stability: composite_score DESC comes first, then
    # deterministic_score, then the two tiebreakers, in that exact order.
    columns_in_order = [c.strip() for c in order_by_clause.split(",")]
    assert columns_in_order == [
        "campaign_candidates.composite_score DESC NULLS LAST",
        "campaign_candidates.deterministic_score DESC",
        "campaign_candidates.created_at ASC",
        "campaign_candidates.id ASC",
    ]


def test_explicit_sort_by_still_ends_with_created_at_asc_id_asc():
    campaign_id = uuid4()
    repo, db = _make_repo([_count_result(0), _rows_result([])])

    repo.get_ranked_by_campaign(campaign_id, page=1, page_size=20, sort_by="semantic_score", sort_order="asc")

    sql = str(db.execute.call_args_list[1].args[0])
    order_by_clause = sql.split("ORDER BY", 1)[1]
    assert "campaign_candidates.semantic_score ASC NULLS LAST" in order_by_clause
    assert "campaign_candidates.created_at ASC" in order_by_clause
    assert "campaign_candidates.id ASC" in order_by_clause
    assert order_by_clause.index("semantic_score") < order_by_clause.index("created_at")


def test_sort_by_ai_score_maps_to_effective_ai_score_column():
    """'ai_score' must sort by effective_ai_score - the same column CompositeScoringService itself reads."""
    campaign_id = uuid4()
    repo, db = _make_repo([_count_result(0), _rows_result([])])

    repo.get_ranked_by_campaign(campaign_id, page=1, page_size=20, sort_by="ai_score", sort_order="desc")

    sql = str(db.execute.call_args_list[1].args[0])
    assert "campaign_candidates.effective_ai_score DESC NULLS LAST" in sql


def test_pagination_uses_limit_and_offset():
    campaign_id = uuid4()
    repo, db = _make_repo([_count_result(0), _rows_result([])])

    repo.get_ranked_by_campaign(campaign_id, page=3, page_size=25)

    main_stmt = db.execute.call_args_list[1].args[0]
    assert main_stmt._limit_clause is not None
    assert main_stmt._offset_clause is not None
    compiled = main_stmt.compile(compile_kwargs={"literal_binds": True})
    sql = str(compiled)
    assert "LIMIT 25" in sql
    assert "OFFSET 50" in sql  # (page 3 - 1) * page_size 25


def test_filters_combine_with_and():
    campaign_id = uuid4()
    repo, db = _make_repo([_count_result(0), _rows_result([])])

    repo.get_ranked_by_campaign(
        campaign_id, page=1, page_size=20,
        pipeline_stage=PipelineStage.SHORTLISTED,
        composite_score_min=50.0,
        composite_score_max=90.0,
        ai_recommendation=AIRecommendation.SHORTLIST,
        ai_evaluation_status=AIEvaluationStatus.COMPLETED,
        include_pending=False,
        include_rejected=False,
        include_fraud=False,
        hr_override=True,
    )

    main_stmt = db.execute.call_args_list[1].args[0]
    sql = str(main_stmt.compile(compile_kwargs={"literal_binds": True}))
    assert " AND " in sql
    assert "campaign_candidates.pipeline_stage = 'SHORTLISTED'" in sql
    assert "campaign_candidates.composite_score >= 50.0" in sql
    assert "campaign_candidates.composite_score <= 90.0" in sql
    assert "campaign_candidates.ai_recommendation = 'SHORTLIST'" in sql
    assert "campaign_candidates.ai_evaluation_status = 'COMPLETED'" in sql
    assert "campaign_candidates.composite_score IS NOT NULL" in sql
    assert "campaign_candidates.pipeline_stage != 'REJECTED'" in sql
    assert "campaign_candidates.is_fraud_flagged IS false" in sql
    assert "campaign_candidates.hr_override IS true" in sql


def test_no_filters_beyond_campaign_id_when_defaults_used():
    """include_pending/rejected/fraud all default True -> no exclusion filters; only campaign_id in WHERE."""
    campaign_id = uuid4()
    repo, db = _make_repo([_count_result(0), _rows_result([])])

    repo.get_ranked_by_campaign(campaign_id, page=1, page_size=20)

    main_stmt = db.execute.call_args_list[1].args[0]
    sql = str(main_stmt.compile(compile_kwargs={"literal_binds": True}))
    where_clause = sql.split("WHERE", 1)[1].split("ORDER BY")[0]
    assert "campaign_id" in where_clause
    assert "IS NOT NULL" not in where_clause
    assert "REJECTED" not in where_clause
    assert "is_fraud_flagged" not in where_clause.lower()


def test_returns_rows_and_total_count():
    campaign_id = uuid4()
    fake_rows = [("cc1", "cand1", "resume1"), ("cc2", "cand2", "resume2")]
    repo, db = _make_repo([_count_result(42), _rows_result(fake_rows)])

    rows, total = repo.get_ranked_by_campaign(campaign_id, page=1, page_size=20)

    assert rows == fake_rows
    assert total == 42


def test_count_query_uses_same_filters_as_main_query():
    campaign_id = uuid4()
    repo, db = _make_repo([_count_result(0), _rows_result([])])

    repo.get_ranked_by_campaign(campaign_id, page=1, page_size=20, pipeline_stage=PipelineStage.REJECTED)

    count_stmt = db.execute.call_args_list[0].args[0]
    sql = str(count_stmt.compile(compile_kwargs={"literal_binds": True}))
    assert "pipeline_stage = 'REJECTED'" in sql


def test_ranking_never_performed_in_python_no_sort_call_on_rows():
    """The rows returned are exactly what the DB driver returned, in that order - never re-sorted."""
    campaign_id = uuid4()
    fake_rows = MagicMock()
    repo, db = _make_repo([_count_result(0), _rows_result(fake_rows)])

    rows, _ = repo.get_ranked_by_campaign(campaign_id, page=1, page_size=20)

    assert rows is fake_rows
    fake_rows.sort.assert_not_called()


# ----------------------------------------------------------------------
# get_score_aggregates
# ----------------------------------------------------------------------

def test_score_aggregates_empty_campaign_returns_zero_counts_and_none_scores():
    """Zero-row campaign: COUNT returns 0 (never NULL); SUM/MAX/MIN/AVG return NULL over empty sets."""
    campaign_id = uuid4()
    result = MagicMock()
    result.one.return_value = (0, 0, None, None, None, None, None, None)
    repo, db = _make_repo([result])

    aggregates = repo.get_score_aggregates(campaign_id)

    assert aggregates == {
        "total": 0, "ranked": 0, "failed": 0, "rejected": 0, "fraud": 0,
        "highest": None, "lowest": None, "average": None,
    }


def test_score_aggregates_maps_row_to_dict():
    campaign_id = uuid4()
    result = MagicMock()
    result.one.return_value = (100, 60, 5, 10, 3, 95.5, 12.25, 54.125)
    repo, db = _make_repo([result])

    aggregates = repo.get_score_aggregates(campaign_id)

    assert aggregates["total"] == 100
    assert aggregates["ranked"] == 60
    assert aggregates["failed"] == 5
    assert aggregates["rejected"] == 10
    assert aggregates["fraud"] == 3
    assert aggregates["highest"] == 95.5
    assert aggregates["lowest"] == 12.25
    assert aggregates["average"] == 54.125


# ----------------------------------------------------------------------
# get_ai_recommendation_counts
# ----------------------------------------------------------------------

def test_ai_recommendation_counts_excludes_null_and_maps_enum_to_value():
    campaign_id = uuid4()
    result = MagicMock()
    result.all.return_value = [(AIRecommendation.SHORTLIST, 7), (AIRecommendation.REJECT, 3)]
    repo, db = _make_repo([result])

    counts = repo.get_ai_recommendation_counts(campaign_id)

    assert counts == {"SHORTLIST": 7, "REJECT": 3}
    stmt = db.execute.call_args[0][0]
    sql = str(stmt.compile(compile_kwargs={"literal_binds": True}))
    assert "ai_recommendation IS NOT NULL" in sql
