from unittest.mock import MagicMock
from uuid import uuid4

import pytest

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
    """
    ai_recommendation/ai_evaluation_status/hr_override are deliberately not
    exercised here - those three columns do not exist on the live
    campaign_candidates table (app/models/pipeline.py no longer maps them),
    so passing any of them raises AttributeError before a statement is even
    built (see test_optional_filters_referencing_removed_columns_raise
    below). Every filter that DOES map to a real column still combines
    correctly.
    """
    campaign_id = uuid4()
    repo, db = _make_repo([_count_result(0), _rows_result([])])

    repo.get_ranked_by_campaign(
        campaign_id, page=1, page_size=20,
        pipeline_stage=PipelineStage.SHORTLISTED,
        composite_score_min=50.0,
        composite_score_max=90.0,
        include_pending=False,
        include_rejected=False,
        include_fraud=False,
    )

    main_stmt = db.execute.call_args_list[1].args[0]
    sql = str(main_stmt.compile(compile_kwargs={"literal_binds": True}))
    assert " AND " in sql
    assert "campaign_candidates.pipeline_stage = 'SHORTLISTED'" in sql
    assert "campaign_candidates.composite_score >= 50.0" in sql
    assert "campaign_candidates.composite_score <= 90.0" in sql
    assert "campaign_candidates.composite_score IS NOT NULL" in sql
    assert "campaign_candidates.pipeline_stage != 'REJECTED'" in sql
    assert "campaign_candidates.is_fraud_flagged IS false" in sql


def test_optional_filters_referencing_removed_columns_raise():
    """
    Documents current, honest behavior rather than papering over it:
    ai_recommendation/ai_evaluation_status/hr_override reference columns
    that were removed from CampaignCandidate because they don't exist on
    the live RDS campaign_candidates table. Fixing these three filters
    properly (or removing them from the method's signature) is a separate,
    pre-existing pipeline concern, not part of this change's scope.
    """
    campaign_id = uuid4()
    repo, _ = _make_repo([_count_result(0), _rows_result([])])

    for kwargs in (
        {"ai_recommendation": AIRecommendation.SHORTLIST},
        {"ai_evaluation_status": AIEvaluationStatus.COMPLETED},
        {"hr_override": True},
    ):
        with pytest.raises(AttributeError):
            repo.get_ranked_by_campaign(campaign_id, page=1, page_size=20, **kwargs)


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

def test_score_aggregates_raises_because_ai_evaluation_status_column_is_removed():
    """
    get_score_aggregates' single aggregate SELECT includes a CASE WHEN ...
    ai_evaluation_status = 'FAILED' clause - ai_evaluation_status does not
    exist on the live campaign_candidates table, so this method was
    already unconditionally broken against this database (Postgres would
    have rejected the whole statement with UndefinedColumn regardless of
    what any other column held). Removing the column mapping just moves
    that same 100%-failure-rate earlier, into Python, before any SQL is
    sent. Fixing this method's reliance on decision_*/other real columns
    instead is a separate, pre-existing pipeline concern, not part of this
    change's scope.
    """
    campaign_id = uuid4()
    repo, _ = _make_repo()

    with pytest.raises(AttributeError):
        repo.get_score_aggregates(campaign_id)


# ----------------------------------------------------------------------
# get_ai_recommendation_counts
# ----------------------------------------------------------------------

def test_ai_recommendation_counts_raises_because_ai_recommendation_column_is_removed():
    """Same reasoning as test_score_aggregates_raises_... - ai_recommendation doesn't exist on the live table."""
    campaign_id = uuid4()
    repo, _ = _make_repo()

    with pytest.raises(AttributeError):
        repo.get_ai_recommendation_counts(campaign_id)
