from unittest.mock import MagicMock
from uuid import uuid4

from app.repositories.campaign_candidate_repository import CampaignCandidateRepository

"""get_candidate_ids_by_campaign - used by Talent Pool search's campaign_id exclusion filter."""


def _make_repo(candidate_ids=None):
    db = MagicMock()
    db.execute.return_value.scalars.return_value.all.return_value = candidate_ids or []
    return CampaignCandidateRepository(db), db


def test_get_candidate_ids_by_campaign_returns_empty_set_when_none_added():
    repo, db = _make_repo()

    result = repo.get_candidate_ids_by_campaign(uuid4())

    assert result == set()
    db.execute.assert_called_once()


def test_get_candidate_ids_by_campaign_returns_the_set_of_candidate_ids():
    candidate_a, candidate_b = uuid4(), uuid4()
    repo, db = _make_repo([candidate_a, candidate_b])

    result = repo.get_candidate_ids_by_campaign(uuid4())

    assert result == {candidate_a, candidate_b}
