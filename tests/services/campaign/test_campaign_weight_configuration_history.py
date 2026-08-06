from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from app.enums.constants import ActionType, EntityType
from app.exceptions.campaign_exceptions import CampaignException
from app.models.campaigns import CampaignStatus
from app.schemas.campaign.campaign_schema import CampaignScoringUpdateRequest, CampaignUpdateRequest
from app.services.campaign.campaign_service import CampaignService

SERVICE_MODULE = "app.services.campaign.campaign_service"


def _make_campaign(
    weight_deterministic=Decimal("30.00"), weight_semantic=Decimal("40.00"), weight_ai=Decimal("30.00"),
    semantic_threshold=Decimal("0.6500"), ai_threshold=Decimal("50.00"), deterministic_threshold=Decimal("70.00"),
    status=CampaignStatus.PAUSED, name="Backend Engineer Q3",
):
    return SimpleNamespace(
        id=uuid4(), org_id=uuid4(), jd_id=uuid4(), name=name, status=status,
        weight_deterministic=weight_deterministic, weight_semantic=weight_semantic, weight_ai=weight_ai,
        semantic_threshold=semantic_threshold, ai_threshold=ai_threshold,
        deterministic_threshold=deterministic_threshold,
        max_candidates=None, deadline=None, prompt_template_id=uuid4(), ai_evaluate_prompt_id=None,
        hiring_manager_id="hm-1", recruiter_id="rec-1", created_by="hr-1",
        created_at=datetime.now(timezone.utc), updated_at=None,
    )


def _make_jd_repo():
    jd_repo = MagicMock()
    jd_repo.get_by_id.return_value = SimpleNamespace(title="Backend Engineer", version_number=1)
    return jd_repo


def _make_service(campaign_repo=None, audit_service=None, history_repo=None, config_repo=None, jd_repo=None):
    return CampaignService(
        campaign_repo=campaign_repo or MagicMock(),
        jd_repo=jd_repo or _make_jd_repo(),
        audit_service=audit_service or MagicMock(),
        config_repo=config_repo or MagicMock(get_configs_by_keys=MagicMock(return_value={})),
        preset_repo=MagicMock(),
        db=MagicMock(),
        circuit_breaker_repo=MagicMock(),
        dead_letter_queue_repo=MagicMock(),
        prompt_template_repo=MagicMock(),
        campaign_weight_configuration_history_repo=history_repo or MagicMock(),
    )


# ----------------------------------------------------------------------
# _validate_scoring_weights - M10-E02 range check (0-100), on top of the
# pre-existing sum=100 and MIN_LAYER_WEIGHT checks.
# ----------------------------------------------------------------------

def test_validate_scoring_weights_rejects_negative_weight_even_when_sum_is_100():
    """A -50/200/-50 triple sums to 100.00 but each individual value is out of range."""
    service = _make_service()

    with pytest.raises(CampaignException):
        service._validate_scoring_weights(Decimal("-50"), Decimal("200"), Decimal("-50"))


def test_validate_scoring_weights_rejects_weight_over_100():
    service = _make_service()

    with pytest.raises(CampaignException):
        service._validate_scoring_weights(Decimal("150"), Decimal("-50"), Decimal("0"))


def test_validate_scoring_weights_accepts_valid_weights():
    service = _make_service()
    # Must not raise.
    service._validate_scoring_weights(Decimal("30.00"), Decimal("40.00"), Decimal("30.00"))


# ----------------------------------------------------------------------
# _record_weight_configuration_change - the shared history + audit helper.
# ----------------------------------------------------------------------

def test_record_weight_configuration_change_creates_immutable_history_row():
    history_repo = MagicMock()
    audit_service = MagicMock()
    service = _make_service(history_repo=history_repo, audit_service=audit_service)
    campaign = _make_campaign(weight_deterministic=Decimal("40.00"), weight_semantic=Decimal("35.00"), weight_ai=Decimal("25.00"))
    old_weights = {
        "weight_deterministic": Decimal("30.00"),
        "weight_semantic": Decimal("40.00"),
        "weight_ai": Decimal("30.00"),
    }

    service._record_weight_configuration_change(campaign, old_weights, "hr-1")

    history_repo.create.assert_called_once()
    history_row = history_repo.create.call_args[0][0]
    assert history_row.campaign_id == campaign.id
    assert history_row.old_weight_deterministic == Decimal("30.00")
    assert history_row.old_weight_semantic == Decimal("40.00")
    assert history_row.old_weight_ai == Decimal("30.00")
    assert history_row.new_weight_deterministic == Decimal("40.00")
    assert history_row.new_weight_semantic == Decimal("35.00")
    assert history_row.new_weight_ai == Decimal("25.00")
    assert history_row.changed_by == "hr-1"
    assert history_row.formula_version == "v1"


def test_record_weight_configuration_change_writes_dedicated_audit_entry():
    from app.enums.constants import COMPOSITE_SCORE_FORMULA_VERSION

    audit_service = MagicMock()
    service = _make_service(audit_service=audit_service)
    campaign = _make_campaign()
    old_weights = {"weight_deterministic": Decimal("50.00"), "weight_semantic": Decimal("30.00"), "weight_ai": Decimal("20.00")}

    service._record_weight_configuration_change(campaign, old_weights, "hr-1")

    audit_service.log.assert_called_once()
    kwargs = audit_service.log.call_args.kwargs
    assert kwargs["action_type"] == ActionType.CAMPAIGN_WEIGHT_CONFIGURATION_CHANGED
    assert kwargs["entity_type"] == EntityType.CAMPAIGN
    assert kwargs["entity_id"] == campaign.id
    assert kwargs["campaign_id"] == campaign.id
    assert kwargs["actor_id"] == "hr-1"
    assert kwargs["details"]["old_weights"]["weight_deterministic"] == "50.00"
    assert kwargs["details"]["new_weights"]["weight_deterministic"] == str(campaign.weight_deterministic)
    assert kwargs["details"]["formula_version"] == COMPOSITE_SCORE_FORMULA_VERSION


# ----------------------------------------------------------------------
# update_scoring_configuration - full method behavior.
# ----------------------------------------------------------------------

def _scoring_request(campaign, **overrides):
    fields = dict(
        weight_deterministic=campaign.weight_deterministic,
        weight_semantic=campaign.weight_semantic,
        weight_ai=campaign.weight_ai,
        semantic_threshold=campaign.semantic_threshold,
        ai_threshold=campaign.ai_threshold,
        deterministic_threshold=campaign.deterministic_threshold,
    )
    fields.update(overrides)
    return CampaignScoringUpdateRequest(**fields)


def _apply_scoring_update(campaign, request):
    """Mirrors CampaignRepository.update_scoring_configuration's real mutation, for mocking."""
    campaign.weight_deterministic = request.weight_deterministic
    campaign.weight_semantic = request.weight_semantic
    campaign.weight_ai = request.weight_ai
    campaign.semantic_threshold = request.semantic_threshold
    campaign.ai_threshold = request.ai_threshold
    campaign.deterministic_threshold = request.deterministic_threshold
    return campaign


def _wire_scoring_configuration_mocks(campaign_repo, campaign):
    campaign_repo.get_by_id_for_update.return_value = campaign
    campaign_repo.get_by_id.return_value = campaign
    campaign_repo.get_candidate_count.return_value = 0
    campaign_repo.update_scoring_configuration.side_effect = _apply_scoring_update


def test_update_scoring_configuration_no_op_skips_history_audit_and_recalculation():
    """
    No-Op Detection: resubmitting exactly the current weights (and
    thresholds) must not insert history, must not write the dedicated
    audit entry, must not trigger recalculation - just return success.
    """
    campaign = _make_campaign()
    campaign_repo = MagicMock()
    _wire_scoring_configuration_mocks(campaign_repo, campaign)
    history_repo = MagicMock()
    audit_service = MagicMock()
    service = _make_service(campaign_repo=campaign_repo, audit_service=audit_service, history_repo=history_repo)
    service._enqueue_composite_recalculation_for_campaign = MagicMock()

    request = _scoring_request(campaign)
    result = service.update_scoring_configuration(campaign.id, request, "hr-1")

    history_repo.create.assert_not_called()
    audit_service.log.assert_not_called()
    service._enqueue_composite_recalculation_for_campaign.assert_not_called()
    campaign_repo.commit.assert_called_once()
    assert result is not None


def test_update_scoring_configuration_records_history_and_audit_and_recalculates_on_weight_change():
    campaign = _make_campaign(weight_deterministic=Decimal("30.00"), weight_semantic=Decimal("40.00"), weight_ai=Decimal("30.00"))
    campaign_repo = MagicMock()
    _wire_scoring_configuration_mocks(campaign_repo, campaign)
    history_repo = MagicMock()
    audit_service = MagicMock()
    service = _make_service(campaign_repo=campaign_repo, audit_service=audit_service, history_repo=history_repo)
    service._enqueue_composite_recalculation_for_campaign = MagicMock()

    request = _scoring_request(campaign, weight_deterministic=Decimal("50.00"), weight_semantic=Decimal("30.00"), weight_ai=Decimal("20.00"))
    service.update_scoring_configuration(campaign.id, request, "hr-1")

    history_repo.create.assert_called_once()
    history_row = history_repo.create.call_args[0][0]
    assert history_row.old_weight_deterministic == Decimal("30.00")
    assert history_row.new_weight_deterministic == Decimal("50.00")

    weight_change_calls = [
        c for c in audit_service.log.call_args_list
        if c.kwargs["action_type"] == ActionType.CAMPAIGN_WEIGHT_CONFIGURATION_CHANGED
    ]
    assert len(weight_change_calls) == 1

    service._enqueue_composite_recalculation_for_campaign.assert_called_once_with(campaign.id)
    campaign_repo.commit.assert_called_once()


def test_update_scoring_configuration_thresholds_only_change_skips_weight_history():
    """A thresholds-only change still writes the existing generic audit entry, but not the weight-specific one, and never recalculates."""
    campaign = _make_campaign()
    campaign_repo = MagicMock()
    _wire_scoring_configuration_mocks(campaign_repo, campaign)
    history_repo = MagicMock()
    audit_service = MagicMock()
    service = _make_service(campaign_repo=campaign_repo, audit_service=audit_service, history_repo=history_repo)
    service._enqueue_composite_recalculation_for_campaign = MagicMock()

    request = _scoring_request(campaign, ai_threshold=Decimal("60.00"))
    service.update_scoring_configuration(campaign.id, request, "hr-1")

    history_repo.create.assert_not_called()
    service._enqueue_composite_recalculation_for_campaign.assert_not_called()
    action_types = [c.kwargs["action_type"] for c in audit_service.log.call_args_list]
    assert ActionType.CAMPAIGN_SCORING_CONFIG_CHANGED in action_types
    assert ActionType.CAMPAIGN_WEIGHT_CONFIGURATION_CHANGED not in action_types


def test_update_scoring_configuration_rejects_invalid_weights_before_any_write():
    """Invalid weights (sum != 100) must abort before touching campaign/history/audit, and roll back."""
    campaign = _make_campaign()
    campaign_repo = MagicMock()
    _wire_scoring_configuration_mocks(campaign_repo, campaign)
    history_repo = MagicMock()
    audit_service = MagicMock()
    service = _make_service(campaign_repo=campaign_repo, audit_service=audit_service, history_repo=history_repo)

    request = _scoring_request(campaign, weight_deterministic=Decimal("50.00"), weight_semantic=Decimal("50.00"), weight_ai=Decimal("50.00"))

    with pytest.raises(CampaignException):
        service.update_scoring_configuration(campaign.id, request, "hr-1")

    campaign_repo.update_scoring_configuration.assert_not_called()
    history_repo.create.assert_not_called()
    audit_service.log.assert_not_called()
    campaign_repo.commit.assert_not_called()
    campaign_repo.rollback.assert_called_once()


def test_update_scoring_configuration_not_found_rolls_back():
    campaign_repo = MagicMock()
    campaign_repo.get_by_id_for_update.return_value = None
    service = _make_service(campaign_repo=campaign_repo)
    request = _scoring_request(_make_campaign())

    with pytest.raises(CampaignException) as exc_info:
        service.update_scoring_configuration(uuid4(), request, "hr-1")

    assert exc_info.value.status_code == 404
    campaign_repo.rollback.assert_called_once()


def test_update_scoring_configuration_uses_locking_read():
    """M10-E02: concurrent weight-change requests must serialize via SELECT ... FOR UPDATE."""
    campaign = _make_campaign()
    campaign_repo = MagicMock()
    _wire_scoring_configuration_mocks(campaign_repo, campaign)
    service = _make_service(campaign_repo=campaign_repo)
    service._enqueue_composite_recalculation_for_campaign = MagicMock()

    service.update_scoring_configuration(campaign.id, _scoring_request(campaign), "hr-1")

    # get_by_id_for_update is the locking read of the campaign being
    # mutated; get_by_id is still legitimately called afterwards by
    # get_scoring_configuration() to build the response - that's a
    # separate, unrelated read, not a second attempt to lock the row.
    campaign_repo.get_by_id_for_update.assert_called_once_with(campaign.id)


# ----------------------------------------------------------------------
# update_campaign - weight-change history/audit path specifically.
# ----------------------------------------------------------------------

def _campaign_update_request(**overrides):
    fields = dict()
    fields.update(overrides)
    return CampaignUpdateRequest(**fields)


def _wire_update_campaign_mocks(campaign_repo, campaign):
    campaign_repo.get_by_id_for_update.return_value = campaign
    campaign_repo.get_candidate_count.return_value = 0
    campaign_repo.get_shortlisted_count.return_value = 0
    campaign_repo.get_by_name.return_value = None
    campaign_repo.get_user.return_value = None  # _resolve_actor falls back to "System"
    campaign_repo.update.side_effect = lambda c: c


def test_update_campaign_weight_change_records_history_and_recalculates():
    campaign = _make_campaign(weight_deterministic=Decimal("30.00"), weight_semantic=Decimal("40.00"), weight_ai=Decimal("30.00"))
    campaign_repo = MagicMock()
    _wire_update_campaign_mocks(campaign_repo, campaign)
    history_repo = MagicMock()
    audit_service = MagicMock()
    service = _make_service(campaign_repo=campaign_repo, audit_service=audit_service, history_repo=history_repo)
    service._enqueue_composite_recalculation_for_campaign = MagicMock()

    request = _campaign_update_request(
        weight_deterministic=Decimal("50.00"), weight_semantic=Decimal("30.00"), weight_ai=Decimal("20.00"),
        confirm_scoring_change=True,
    )
    service.update_campaign(campaign.id, request, "hr-1")

    history_repo.create.assert_called_once()
    history_row = history_repo.create.call_args[0][0]
    assert history_row.old_weight_deterministic == Decimal("30.00")
    assert history_row.new_weight_deterministic == Decimal("50.00")
    service._enqueue_composite_recalculation_for_campaign.assert_called_once_with(campaign.id)


def test_update_campaign_no_op_weight_resubmission_skips_history():
    """Resubmitting the campaign's current weights via the generic PATCH must not create history/audit/recalculation."""
    campaign = _make_campaign()
    campaign_repo = MagicMock()
    _wire_update_campaign_mocks(campaign_repo, campaign)
    history_repo = MagicMock()
    service = _make_service(campaign_repo=campaign_repo, history_repo=history_repo)
    service._enqueue_composite_recalculation_for_campaign = MagicMock()

    request = _campaign_update_request(
        weight_deterministic=campaign.weight_deterministic,
        weight_semantic=campaign.weight_semantic,
        weight_ai=campaign.weight_ai,
        name="A brand new name",  # ensures `changes` is non-empty so the method doesn't 422 on "no changes"
    )
    service.update_campaign(campaign.id, request, "hr-1")

    history_repo.create.assert_not_called()
    service._enqueue_composite_recalculation_for_campaign.assert_not_called()


def test_update_campaign_uses_locking_read():
    campaign = _make_campaign()
    campaign_repo = MagicMock()
    _wire_update_campaign_mocks(campaign_repo, campaign)
    service = _make_service(campaign_repo=campaign_repo)

    service.update_campaign(campaign.id, _campaign_update_request(name="New Name"), "hr-1")

    campaign_repo.get_by_id_for_update.assert_called_once_with(campaign.id)
    campaign_repo.get_by_id.assert_not_called()


def test_update_campaign_rolls_back_and_skips_recalculation_on_invalid_weights():
    campaign = _make_campaign()
    campaign_repo = MagicMock()
    _wire_update_campaign_mocks(campaign_repo, campaign)
    history_repo = MagicMock()
    service = _make_service(campaign_repo=campaign_repo, history_repo=history_repo)
    service._enqueue_composite_recalculation_for_campaign = MagicMock()

    request = _campaign_update_request(
        weight_deterministic=Decimal("60.00"), weight_semantic=Decimal("60.00"), weight_ai=Decimal("60.00"),
        confirm_scoring_change=True,
    )

    with pytest.raises(CampaignException):
        service.update_campaign(campaign.id, request, "hr-1")

    history_repo.create.assert_not_called()
    service._enqueue_composite_recalculation_for_campaign.assert_not_called()
    campaign_repo.commit.assert_not_called()
    campaign_repo.rollback.assert_called_once()


# ----------------------------------------------------------------------
# CampaignWeightConfigurationHistoryRepository - immutability contract.
# ----------------------------------------------------------------------

def test_campaign_weight_configuration_history_repository_has_no_update_or_delete():
    """History rows are append-only - the repository must never expose a mutation path."""
    from app.repositories.campaign_weight_configuration_history_repository import (
        CampaignWeightConfigurationHistoryRepository,
    )

    assert not hasattr(CampaignWeightConfigurationHistoryRepository, "update")
    assert not hasattr(CampaignWeightConfigurationHistoryRepository, "delete")


def test_campaign_weight_configuration_history_repository_create_flushes_and_refreshes():
    from app.repositories.campaign_weight_configuration_history_repository import (
        CampaignWeightConfigurationHistoryRepository,
    )

    db = MagicMock()
    repo = CampaignWeightConfigurationHistoryRepository(db)
    history = MagicMock()

    result = repo.create(history)

    db.add.assert_called_once_with(history)
    db.flush.assert_called_once()
    db.refresh.assert_called_once_with(history)
    assert result is history
