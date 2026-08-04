from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from app.exceptions.campaign_exceptions import CampaignException
from app.models.pipeline import AIEvaluationStatus, AIRecommendation, PipelineStage
from app.services.campaign.campaign_candidate_service import CampaignCandidateService


def make_service(campaign_repo=None, campaign_candidate_repo=None, audit_service=None):
    return CampaignCandidateService(
        campaign_repo=campaign_repo or MagicMock(),
        campaign_candidate_repo=campaign_candidate_repo or MagicMock(),
        audit_service=audit_service or MagicMock(),
    )


def _make_campaign_candidate(
    composite_score=None, deterministic_score=None, ai_evaluation_status=AIEvaluationStatus.PENDING,
    pipeline_stage=PipelineStage.SCREENING, is_fraud_flagged=False, hr_override=False,
    ai_recommendation=None,
):
    return SimpleNamespace(
        id=uuid4(), campaign_id=uuid4(), candidate_id=uuid4(), resume_id=uuid4(),
        pipeline_stage=pipeline_stage, composite_score=composite_score,
        deterministic_score=deterministic_score, ai_ats_score=None, semantic_score=None,
        ai_evaluation_status=ai_evaluation_status, is_fraud_flagged=is_fraud_flagged,
        hr_override=hr_override, ai_recommendation=ai_recommendation,
        created_at=datetime.now(timezone.utc),
    )


def _make_campaign():
    return SimpleNamespace(id=uuid4())


# ----------------------------------------------------------------------
# _derive_ranking_status
# ----------------------------------------------------------------------

def test_ranking_status_is_ranked_when_composite_score_present():
    cc = _make_campaign_candidate(composite_score=72.5)
    assert CampaignCandidateService._derive_ranking_status(cc) == "RANKED"


def test_ranking_status_is_ranked_even_when_rejected():
    """A candidate scored before being rejected keeps its RANKED status - rejection is a separate concern."""
    cc = _make_campaign_candidate(composite_score=40.0, pipeline_stage=PipelineStage.REJECTED)
    assert CampaignCandidateService._derive_ranking_status(cc) == "RANKED"


def test_ranking_status_is_failed_when_no_score_and_ai_evaluation_failed():
    cc = _make_campaign_candidate(composite_score=None, ai_evaluation_status=AIEvaluationStatus.FAILED)
    assert CampaignCandidateService._derive_ranking_status(cc) == "FAILED"


@pytest.mark.parametrize("status", [
    AIEvaluationStatus.PENDING, AIEvaluationStatus.IN_PROGRESS,
    AIEvaluationStatus.SKIPPED, AIEvaluationStatus.MANUAL_REVIEW, AIEvaluationStatus.COMPLETED,
])
def test_ranking_status_is_pending_for_every_other_no_score_case(status):
    cc = _make_campaign_candidate(composite_score=None, ai_evaluation_status=status)
    assert CampaignCandidateService._derive_ranking_status(cc) == "PENDING"


# ----------------------------------------------------------------------
# get_ranked_campaign_candidates
# ----------------------------------------------------------------------

def test_raises_when_campaign_not_found():
    campaign_repo = MagicMock()
    campaign_repo.get_by_id.return_value = None
    service = make_service(campaign_repo=campaign_repo)

    with pytest.raises(CampaignException) as exc_info:
        service.get_ranked_campaign_candidates(uuid4())

    assert exc_info.value.status_code == 404


@pytest.mark.parametrize("page", [0, -1])
def test_rejects_invalid_page(page):
    campaign_repo = MagicMock()
    campaign_repo.get_by_id.return_value = _make_campaign()
    service = make_service(campaign_repo=campaign_repo)

    with pytest.raises(CampaignException) as exc_info:
        service.get_ranked_campaign_candidates(uuid4(), page=page)

    assert exc_info.value.status_code == 422


@pytest.mark.parametrize("page_size", [0, -5, 101, 1000])
def test_rejects_invalid_page_size(page_size):
    campaign_repo = MagicMock()
    campaign_repo.get_by_id.return_value = _make_campaign()
    service = make_service(campaign_repo=campaign_repo)

    with pytest.raises(CampaignException) as exc_info:
        service.get_ranked_campaign_candidates(uuid4(), page_size=page_size)

    assert exc_info.value.status_code == 422


def test_rejects_composite_score_min_greater_than_max():
    campaign_repo = MagicMock()
    campaign_repo.get_by_id.return_value = _make_campaign()
    service = make_service(campaign_repo=campaign_repo)

    with pytest.raises(CampaignException) as exc_info:
        service.get_ranked_campaign_candidates(uuid4(), composite_score_min=80, composite_score_max=20)

    assert exc_info.value.status_code == 422


def test_empty_campaign_returns_empty_page_with_correct_total():
    campaign_repo = MagicMock()
    campaign_repo.get_by_id.return_value = _make_campaign()
    campaign_candidate_repo = MagicMock()
    campaign_candidate_repo.get_ranked_by_campaign.return_value = ([], 0)
    service = make_service(campaign_repo=campaign_repo, campaign_candidate_repo=campaign_candidate_repo)

    result = service.get_ranked_campaign_candidates(uuid4())

    assert result.items == []
    assert result.total == 0
    assert result.page == 1


def test_ranks_are_1_based_and_offset_by_page():
    campaign_repo = MagicMock()
    campaign_repo.get_by_id.return_value = _make_campaign()
    ccs = [_make_campaign_candidate(composite_score=90 - i) for i in range(3)]
    campaign_candidate_repo = MagicMock()
    campaign_candidate_repo.get_ranked_by_campaign.return_value = (
        [(cc, None, None) for cc in ccs], 23,
    )
    service = make_service(campaign_repo=campaign_repo, campaign_candidate_repo=campaign_candidate_repo)

    result = service.get_ranked_campaign_candidates(uuid4(), page=3, page_size=10)

    assert [item.rank for item in result.items] == [21, 22, 23]
    assert result.total == 23
    assert result.page == 3
    assert result.page_size == 10


def test_ranking_status_and_flags_populated_on_each_item():
    campaign_repo = MagicMock()
    campaign_repo.get_by_id.return_value = _make_campaign()
    cc = _make_campaign_candidate(
        composite_score=None, ai_evaluation_status=AIEvaluationStatus.FAILED,
        is_fraud_flagged=True, hr_override=True, ai_recommendation=AIRecommendation.HOLD,
    )
    campaign_candidate_repo = MagicMock()
    campaign_candidate_repo.get_ranked_by_campaign.return_value = ([(cc, None, None)], 1)
    service = make_service(campaign_repo=campaign_repo, campaign_candidate_repo=campaign_candidate_repo)

    result = service.get_ranked_campaign_candidates(uuid4())

    item = result.items[0]
    assert item.ranking_status == "FAILED"
    assert item.is_fraud_flagged is True
    assert item.hr_override is True
    assert item.ai_recommendation == AIRecommendation.HOLD


def test_passes_all_filters_through_to_repository():
    campaign_repo = MagicMock()
    campaign_repo.get_by_id.return_value = _make_campaign()
    campaign_candidate_repo = MagicMock()
    campaign_candidate_repo.get_ranked_by_campaign.return_value = ([], 0)
    service = make_service(campaign_repo=campaign_repo, campaign_candidate_repo=campaign_candidate_repo)
    campaign_id = uuid4()

    service.get_ranked_campaign_candidates(
        campaign_id, page=2, page_size=15, sort_by="semantic_score", sort_order="asc",
        pipeline_stage=PipelineStage.SHORTLISTED, composite_score_min=10, composite_score_max=90,
        ai_recommendation=AIRecommendation.SHORTLIST, ai_evaluation_status=AIEvaluationStatus.COMPLETED,
        include_pending=False, include_rejected=False, include_fraud=False, hr_override=True,
    )

    campaign_candidate_repo.get_ranked_by_campaign.assert_called_once_with(
        campaign_id, page=2, page_size=15, sort_by="semantic_score", sort_order="asc",
        pipeline_stage=PipelineStage.SHORTLISTED, composite_score_min=10, composite_score_max=90,
        ai_recommendation=AIRecommendation.SHORTLIST, ai_evaluation_status=AIEvaluationStatus.COMPLETED,
        include_pending=False, include_rejected=False, include_fraud=False, hr_override=True,
    )


def test_never_writes_an_audit_entry():
    """Viewing ranked candidates is read-only - must never be audited."""
    campaign_repo = MagicMock()
    campaign_repo.get_by_id.return_value = _make_campaign()
    campaign_candidate_repo = MagicMock()
    campaign_candidate_repo.get_ranked_by_campaign.return_value = ([], 0)
    audit_service = MagicMock()
    service = make_service(
        campaign_repo=campaign_repo, campaign_candidate_repo=campaign_candidate_repo, audit_service=audit_service,
    )

    service.get_ranked_campaign_candidates(uuid4())

    audit_service.log.assert_not_called()


# ----------------------------------------------------------------------
# get_campaign_candidate_summary
# ----------------------------------------------------------------------

def test_summary_raises_when_campaign_not_found():
    campaign_repo = MagicMock()
    campaign_repo.get_by_id.return_value = None
    service = make_service(campaign_repo=campaign_repo)

    with pytest.raises(CampaignException) as exc_info:
        service.get_campaign_candidate_summary(uuid4())

    assert exc_info.value.status_code == 404


def test_summary_computes_pending_as_total_minus_ranked_minus_failed():
    campaign_repo = MagicMock()
    campaign_repo.get_by_id.return_value = _make_campaign()
    campaign_repo.get_stage_counts.return_value = {"SCREENING": 10, "REJECTED": 5}
    campaign_candidate_repo = MagicMock()
    campaign_candidate_repo.get_score_aggregates.return_value = {
        "total": 20, "ranked": 12, "failed": 2, "rejected": 5, "fraud": 1,
        "highest": 95.0, "lowest": 40.0, "average": 67.333,
    }
    campaign_candidate_repo.get_ai_recommendation_counts.return_value = {"SHORTLIST": 8}
    service = make_service(campaign_repo=campaign_repo, campaign_candidate_repo=campaign_candidate_repo)

    summary = service.get_campaign_candidate_summary(uuid4())

    assert summary.total_candidates == 20
    assert summary.ranked_candidates == 12
    assert summary.pending_candidates == 6  # 20 - 12 - 2
    assert summary.rejected_candidates == 5  # from stage_counts, not a second query
    assert summary.fraud_candidates == 1
    assert summary.highest_composite_score == 95.0
    assert summary.lowest_composite_score == 40.0
    assert summary.average_composite_score == 67.33  # rounded to 2 dp
    assert summary.pipeline_stage_counts == {"SCREENING": 10, "REJECTED": 5}
    assert summary.ai_recommendation_counts == {"SHORTLIST": 8}


def test_summary_empty_campaign_returns_zeroed_breakdown():
    campaign_repo = MagicMock()
    campaign_repo.get_by_id.return_value = _make_campaign()
    campaign_repo.get_stage_counts.return_value = {}
    campaign_candidate_repo = MagicMock()
    campaign_candidate_repo.get_score_aggregates.return_value = {
        "total": 0, "ranked": 0, "failed": 0, "rejected": 0, "fraud": 0,
        "highest": None, "lowest": None, "average": None,
    }
    campaign_candidate_repo.get_ai_recommendation_counts.return_value = {}
    service = make_service(campaign_repo=campaign_repo, campaign_candidate_repo=campaign_candidate_repo)

    summary = service.get_campaign_candidate_summary(uuid4())

    assert summary.total_candidates == 0
    assert summary.pending_candidates == 0
    assert summary.average_composite_score is None
    assert summary.pipeline_stage_counts == {}
    assert summary.ai_recommendation_counts == {}


def test_summary_never_writes_an_audit_entry():
    campaign_repo = MagicMock()
    campaign_repo.get_by_id.return_value = _make_campaign()
    campaign_repo.get_stage_counts.return_value = {}
    campaign_candidate_repo = MagicMock()
    campaign_candidate_repo.get_score_aggregates.return_value = {
        "total": 0, "ranked": 0, "failed": 0, "rejected": 0, "fraud": 0,
        "highest": None, "lowest": None, "average": None,
    }
    campaign_candidate_repo.get_ai_recommendation_counts.return_value = {}
    audit_service = MagicMock()
    service = make_service(
        campaign_repo=campaign_repo, campaign_candidate_repo=campaign_candidate_repo, audit_service=audit_service,
    )

    service.get_campaign_candidate_summary(uuid4())

    audit_service.log.assert_not_called()


def test_get_campaign_candidates_unchanged_for_backward_compatibility():
    """The pre-existing, unpaginated get_campaign_candidates() must keep working exactly as before."""
    campaign_repo = MagicMock()
    campaign_repo.get_by_id.return_value = _make_campaign()
    cc = _make_campaign_candidate(composite_score=88.0)
    campaign_candidate_repo = MagicMock()
    campaign_candidate_repo.get_all_by_campaign.return_value = [(cc, None, None)]
    service = make_service(campaign_repo=campaign_repo, campaign_candidate_repo=campaign_candidate_repo)

    result = service.get_campaign_candidates(uuid4())

    assert isinstance(result, list)
    assert result[0].composite_score == 88.0
    assert result[0].rank is None  # no ranking context outside the ranked-list method
