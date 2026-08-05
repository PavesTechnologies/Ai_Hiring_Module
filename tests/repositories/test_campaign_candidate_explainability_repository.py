from unittest.mock import MagicMock
from uuid import uuid4

from app.repositories.campaign_candidate_repository import CampaignCandidateRepository


def _rows_result(rows):
    result = MagicMock()
    result.scalars.return_value.all.return_value = rows
    return result


def test_get_stage_history_by_campaign_candidate_id_filters_by_candidate_and_orders_ascending():
    """
    M10-E03 Phase 2: the Candidate Timeline must be scoped to ONE candidate
    (unlike CampaignRepository.get_stage_history, which is campaign-wide)
    and ordered oldest-first (ascending changed_at), matching this
    service's "Timeline" naming convention.
    """
    campaign_candidate_id = uuid4()
    db = MagicMock()
    fake_rows = [MagicMock(), MagicMock()]
    db.execute.return_value = _rows_result(fake_rows)
    repo = CampaignCandidateRepository(db)

    result = repo.get_stage_history_by_campaign_candidate_id(campaign_candidate_id)

    assert result == fake_rows
    stmt = db.execute.call_args[0][0]
    sql = str(stmt.compile(compile_kwargs={"literal_binds": True}))
    assert "campaign_candidate_stage_history.campaign_candidate_id = " in sql
    assert campaign_candidate_id.hex in sql.replace("-", "")
    assert "ORDER BY campaign_candidate_stage_history.changed_at ASC" in sql


def test_get_stage_history_by_campaign_candidate_id_returns_empty_list_for_no_history():
    db = MagicMock()
    db.execute.return_value = _rows_result([])
    repo = CampaignCandidateRepository(db)

    result = repo.get_stage_history_by_campaign_candidate_id(uuid4())

    assert result == []
