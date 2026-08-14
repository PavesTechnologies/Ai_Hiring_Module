from unittest.mock import MagicMock
from uuid import uuid4

from app.models.pipeline import PipelineStage
from app.repositories.campaign_candidate_repository import CampaignCandidateRepository


def _make_repo(rows=None):
    db = MagicMock()
    result = MagicMock()
    result.all.return_value = rows or []
    db.execute.return_value = result
    return CampaignCandidateRepository(db), db


def test_get_campaign_usage_by_resume_ids_returns_empty_list_without_querying_when_no_ids():
    repo, db = _make_repo()

    result = repo.get_campaign_usage_by_resume_ids([])

    assert result == []
    db.execute.assert_not_called()


def test_get_campaign_usage_by_resume_ids_returns_rows_from_the_join():
    resume_id = uuid4()
    campaign_id = uuid4()
    rows = [(resume_id, campaign_id, "Backend Engineer", PipelineStage.SCREENING)]
    repo, db = _make_repo(rows)

    result = repo.get_campaign_usage_by_resume_ids([resume_id])

    assert result == rows
    db.execute.assert_called_once()


def test_get_campaign_usage_by_resume_ids_can_return_multiple_campaigns_for_one_resume():
    """A resume reused across campaigns via 'use existing' duplicate resolution can appear more than once."""
    resume_id = uuid4()
    rows = [
        (resume_id, uuid4(), "Backend Engineer", PipelineStage.SCREENING),
        (resume_id, uuid4(), "Frontend Engineer", PipelineStage.INTERVIEW),
    ]
    repo, db = _make_repo(rows)

    result = repo.get_campaign_usage_by_resume_ids([resume_id])

    assert len(result) == 2
