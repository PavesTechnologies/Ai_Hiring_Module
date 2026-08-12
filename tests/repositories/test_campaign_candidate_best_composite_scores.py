from unittest.mock import MagicMock
from uuid import uuid4

from app.repositories.campaign_candidate_repository import CampaignCandidateRepository

"""
M13-E01 S02 T0x - CampaignCandidateRepository.get_best_composite_scores_by_candidate_ids.
MAX(composite_score) per candidate, batched across a whole page of
candidates in one query - the Talent Pool card's "best historical score".
"""


def test_returns_max_score_per_candidate():
    candidate_a, candidate_b = uuid4(), uuid4()
    db = MagicMock()
    db.execute.return_value.all.return_value = [(candidate_a, 92.5), (candidate_b, 61.0)]
    repo = CampaignCandidateRepository(db)

    result = repo.get_best_composite_scores_by_candidate_ids([candidate_a, candidate_b])

    assert result == {candidate_a: 92.5, candidate_b: 61.0}
    db.execute.assert_called_once()


def test_candidate_with_no_campaign_score_is_absent_from_result():
    """MAX() over an all-NULL group returns NULL - must not surface as 0."""
    candidate_id = uuid4()
    db = MagicMock()
    db.execute.return_value.all.return_value = [(candidate_id, None)]
    repo = CampaignCandidateRepository(db)

    result = repo.get_best_composite_scores_by_candidate_ids([candidate_id])

    assert candidate_id not in result
    assert result.get(candidate_id) is None


def test_empty_candidate_id_list_short_circuits_without_a_query():
    db = MagicMock()
    repo = CampaignCandidateRepository(db)

    result = repo.get_best_composite_scores_by_candidate_ids([])

    assert result == {}
    db.execute.assert_not_called()
