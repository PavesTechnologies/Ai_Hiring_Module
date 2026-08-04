from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from app.models.pipeline import CompositeScoreTriggerSource
from app.services.campaign.composite_scoring_service import (
    CompositeScoringService,
    InvalidScoreRangeError,
    InvalidScoringWeightsError,
)


def _make_campaign_candidate(
    deterministic_score=None, semantic_score=None, effective_ai_score=None, campaign_id=None,
):
    return SimpleNamespace(
        id=uuid4(),
        campaign_id=campaign_id or uuid4(),
        deterministic_score=deterministic_score,
        semantic_score=semantic_score,
        effective_ai_score=effective_ai_score,
        composite_score=None,
        composite_score_computed_at=None,
    )


def _make_campaign(weight_deterministic="30.00", weight_semantic="40.00", weight_ai="30.00"):
    return SimpleNamespace(
        id=uuid4(),
        weight_deterministic=Decimal(weight_deterministic),
        weight_semantic=Decimal(weight_semantic),
        weight_ai=Decimal(weight_ai),
    )


def _harness(campaign_candidate=None, campaign=None):
    campaign = campaign or _make_campaign()
    campaign_candidate = campaign_candidate or _make_campaign_candidate(campaign_id=campaign.id)

    campaign_candidate_repo = MagicMock()
    campaign_candidate_repo.get_by_id.return_value = campaign_candidate

    campaign_repo = MagicMock()
    campaign_repo.get_by_id.return_value = campaign

    history_repo = MagicMock()
    audit_service = MagicMock()

    service = CompositeScoringService(campaign_candidate_repo, campaign_repo, history_repo, audit_service)
    return service, campaign_candidate, campaign, campaign_candidate_repo, history_repo, audit_service


def test_raises_when_campaign_candidate_not_found():
    service, *_ = _harness()
    service.campaign_candidate_repository.get_by_id.return_value = None

    with pytest.raises(ValueError):
        service.calculate_and_store_composite_score(uuid4(), CompositeScoreTriggerSource.AI_EVALUATION)


def test_raises_when_campaign_not_found():
    service, cc, *_ = _harness()
    service.campaign_repository.get_by_id.return_value = None

    with pytest.raises(ValueError):
        service.calculate_and_store_composite_score(cc.id, CompositeScoreTriggerSource.AI_EVALUATION)


def test_raises_invalid_scoring_weights_error_when_weights_do_not_sum_to_100():
    campaign = _make_campaign(weight_deterministic="30.00", weight_semantic="40.00", weight_ai="20.00")
    cc = _make_campaign_candidate(deterministic_score=80, campaign_id=campaign.id)
    service, *_ = _harness(campaign_candidate=cc, campaign=campaign)

    with pytest.raises(InvalidScoringWeightsError):
        service.calculate_and_store_composite_score(cc.id, CompositeScoreTriggerSource.AI_EVALUATION)


@pytest.mark.parametrize("deterministic_score", [-1, 100.01, 150])
def test_raises_invalid_score_range_error_for_out_of_range_deterministic_score(deterministic_score):
    campaign = _make_campaign()
    cc = _make_campaign_candidate(deterministic_score=deterministic_score, campaign_id=campaign.id)
    service, *_ = _harness(campaign_candidate=cc, campaign=campaign)

    with pytest.raises(InvalidScoreRangeError):
        service.calculate_and_store_composite_score(cc.id, CompositeScoreTriggerSource.AI_EVALUATION)


@pytest.mark.parametrize("semantic_score", [-0.01, 1.01, 2])
def test_raises_invalid_score_range_error_for_out_of_range_semantic_score(semantic_score):
    campaign = _make_campaign()
    cc = _make_campaign_candidate(semantic_score=Decimal(str(semantic_score)), campaign_id=campaign.id)
    service, *_ = _harness(campaign_candidate=cc, campaign=campaign)

    with pytest.raises(InvalidScoreRangeError):
        service.calculate_and_store_composite_score(cc.id, CompositeScoreTriggerSource.AI_EVALUATION)


@pytest.mark.parametrize("effective_ai_score", [-5, 101, 200])
def test_raises_invalid_score_range_error_for_out_of_range_ai_score(effective_ai_score):
    campaign = _make_campaign()
    cc = _make_campaign_candidate(effective_ai_score=effective_ai_score, campaign_id=campaign.id)
    service, *_ = _harness(campaign_candidate=cc, campaign=campaign)

    with pytest.raises(InvalidScoreRangeError):
        service.calculate_and_store_composite_score(cc.id, CompositeScoreTriggerSource.AI_EVALUATION)


def test_all_components_present_uses_configured_weights_unchanged():
    campaign = _make_campaign(weight_deterministic="30.00", weight_semantic="40.00", weight_ai="30.00")
    cc = _make_campaign_candidate(
        deterministic_score=80, semantic_score=Decimal("0.70"), effective_ai_score=60, campaign_id=campaign.id,
    )
    service, cc, campaign, cc_repo, history_repo, audit_service = _harness(campaign_candidate=cc, campaign=campaign)

    breakdown = service.calculate_and_store_composite_score(cc.id, CompositeScoreTriggerSource.AI_EVALUATION)

    # (30*80 + 40*70 + 30*60) / 100 = 70.00
    assert breakdown["composite_score"] == 70.0
    assert breakdown["weight_deterministic"] == 30.0
    assert breakdown["weight_semantic"] == 40.0
    assert breakdown["weight_ai"] == 30.0
    cc_repo.update.assert_called_once_with(cc)
    history_repo.create.assert_called_once()
    audit_service.log.assert_called_once()


def test_missing_ai_score_is_coalesced_to_zero_weights_never_redistributed():
    """
    Fix 3/4: NO weight redistribution. A missing component's raw score is
    treated as 0 (COALESCE semantics); the campaign's configured weights
    are used exactly as-is.
    """
    campaign = _make_campaign(weight_deterministic="30.00", weight_semantic="40.00", weight_ai="30.00")
    cc = _make_campaign_candidate(deterministic_score=80, semantic_score=Decimal("0.70"), campaign_id=campaign.id)
    service, cc, campaign, *_ = _harness(campaign_candidate=cc, campaign=campaign)

    breakdown = service.calculate_and_store_composite_score(cc.id, CompositeScoreTriggerSource.AI_EVALUATION)

    # Weights stay 30/40/30 - AI's weight is NOT redistributed to the other two.
    assert breakdown["weight_deterministic"] == 30.0
    assert breakdown["weight_semantic"] == 40.0
    assert breakdown["weight_ai"] == 30.0
    # (30*80 + 40*70 + 30*0) / 100 = 52.00 (AI contributes 0, not excluded from the denominator)
    assert breakdown["composite_score"] == 52.0


def test_all_components_missing_computes_zero_not_an_error():
    """
    Fix 4/5: all three missing is NOT an error - COALESCE(NULL, 0) for
    every component simply yields a composite score of 0.00.
    """
    campaign = _make_campaign()
    cc = _make_campaign_candidate(campaign_id=campaign.id)
    service, cc, *_ = _harness(campaign_candidate=cc, campaign=campaign)

    breakdown = service.calculate_and_store_composite_score(cc.id, CompositeScoreTriggerSource.AI_EVALUATION)

    assert breakdown["composite_score"] == 0.0


def test_composite_score_is_rounded_to_two_decimal_places():
    campaign = _make_campaign(weight_deterministic="33.00", weight_semantic="34.00", weight_ai="33.00")
    cc = _make_campaign_candidate(
        deterministic_score=Decimal("77.777"), semantic_score=Decimal("0.66666"),
        effective_ai_score=Decimal("55.555"), campaign_id=campaign.id,
    )
    service, cc, *_ = _harness(campaign_candidate=cc, campaign=campaign)

    breakdown = service.calculate_and_store_composite_score(cc.id, CompositeScoreTriggerSource.AI_EVALUATION)

    # Only the final value is rounded - exactly 2 decimal places.
    assert breakdown["composite_score"] == round(breakdown["composite_score"], 2)


def test_semantic_score_normalized_from_0_1_scale_to_0_100_scale():
    campaign = _make_campaign(weight_deterministic="0.00", weight_semantic="100.00", weight_ai="0.00")
    cc = _make_campaign_candidate(semantic_score=Decimal("0.65"), campaign_id=campaign.id)
    service, cc, *_ = _harness(campaign_candidate=cc, campaign=campaign)

    breakdown = service.calculate_and_store_composite_score(cc.id, CompositeScoreTriggerSource.AI_EVALUATION)

    assert breakdown["composite_score"] == 65.0
    assert breakdown["normalized_semantic_score"] == 65.0


def test_persists_composite_score_and_computed_at_on_campaign_candidate():
    campaign = _make_campaign()
    cc = _make_campaign_candidate(deterministic_score=90, campaign_id=campaign.id)
    service, cc, *_ = _harness(campaign_candidate=cc, campaign=campaign)

    service.calculate_and_store_composite_score(cc.id, CompositeScoreTriggerSource.CAMPAIGN_WEIGHT_CHANGE)

    assert cc.composite_score is not None
    assert cc.composite_score_computed_at is not None


def test_history_row_captures_raw_and_normalized_semantic_score_and_configured_weights():
    campaign = _make_campaign(weight_deterministic="30.00", weight_semantic="40.00", weight_ai="30.00")
    cc = _make_campaign_candidate(deterministic_score=90, semantic_score=Decimal("0.5"), campaign_id=campaign.id)
    service, cc, campaign, _, history_repo, _ = _harness(campaign_candidate=cc, campaign=campaign)

    service.calculate_and_store_composite_score(cc.id, CompositeScoreTriggerSource.CAMPAIGN_WEIGHT_CHANGE)

    history_row = history_repo.create.call_args[0][0]
    assert history_row.campaign_candidate_id == cc.id
    assert history_row.deterministic_score == cc.deterministic_score
    assert history_row.semantic_score == cc.semantic_score
    assert history_row.normalized_semantic_score == Decimal("50.0")
    assert history_row.weight_deterministic == Decimal("30.00")
    assert history_row.weight_semantic == Decimal("40.00")
    assert history_row.weight_ai == Decimal("30.00")
    assert history_row.trigger_source == CompositeScoreTriggerSource.CAMPAIGN_WEIGHT_CHANGE
    assert history_row.formula_version == "v1"


def test_history_row_normalized_semantic_score_is_none_when_semantic_score_missing():
    campaign = _make_campaign()
    cc = _make_campaign_candidate(deterministic_score=90, campaign_id=campaign.id)
    service, cc, campaign, _, history_repo, _ = _harness(campaign_candidate=cc, campaign=campaign)

    service.calculate_and_store_composite_score(cc.id, CompositeScoreTriggerSource.AI_EVALUATION)

    history_row = history_repo.create.call_args[0][0]
    assert history_row.semantic_score is None
    assert history_row.normalized_semantic_score is None


def test_audit_log_records_composite_score_computed():
    from app.enums.constants import ActionType, EntityType

    campaign = _make_campaign()
    cc = _make_campaign_candidate(deterministic_score=90, campaign_id=campaign.id)
    service, cc, campaign, _, _, audit_service = _harness(campaign_candidate=cc, campaign=campaign)

    service.calculate_and_store_composite_score(cc.id, CompositeScoreTriggerSource.AI_EVALUATION)

    audit_kwargs = audit_service.log.call_args.kwargs
    assert audit_kwargs["action_type"] == ActionType.COMPOSITE_SCORE_COMPUTED
    assert audit_kwargs["entity_type"] == EntityType.CAMPAIGN_CANDIDATE
    assert audit_kwargs["entity_id"] == cc.id
    assert audit_kwargs["campaign_id"] == campaign.id


# ----------------------------------------------------------------------
# Fix 7: modular method structure - each responsibility independently
# callable/testable, not one large method.
# ----------------------------------------------------------------------

def test_normalize_scores_coalesces_missing_components_to_zero():
    cc = _make_campaign_candidate()
    result = CompositeScoringService.normalize_scores(cc)

    assert result["deterministic"] == Decimal("0")
    assert result["semantic_normalized"] == Decimal("0")
    assert result["ai"] == Decimal("0")


def test_calculate_score_uses_configured_weights_directly():
    weights = {"deterministic": Decimal("50"), "semantic": Decimal("30"), "ai": Decimal("20")}
    normalized_scores = {"deterministic": Decimal("100"), "semantic_normalized": Decimal("0"), "ai": Decimal("0")}

    result = CompositeScoringService.calculate_score(weights, normalized_scores)

    assert result == Decimal("50")


def test_round_score_rounds_to_two_decimal_places():
    assert CompositeScoringService.round_score(Decimal("67.4567")) == 67.46
