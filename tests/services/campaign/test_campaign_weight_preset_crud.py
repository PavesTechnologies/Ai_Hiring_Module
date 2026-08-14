"""
Epic 3 Fix 1 (Pattern B): create_weight_preset/update_weight_preset/
delete_weight_preset each used to commit the preset change, THEN call
audit_service.log(...) + a second independent commit
(audit_service.repository.save()) - a failed or unwritten audit entry left
the preset change durably saved anyway, with zero audit trail of it.

Fixed to match StageTransitionService.transition()'s established pattern:
audit write happens before the only commit, inside the same transaction -
a failed audit write means the preset change never happened either. No
repo-level change was needed - CampaignWeightPresetRepository.create/
update/delete already only flush(), never commit() internally.
"""
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from app.models.campaign_weight_preset import CampaignWeightPreset
from app.schemas.campaign.campaign_weight_preset_schema import (
    CampaignWeightPresetCreateRequest,
    CampaignWeightPresetResponse,
    CampaignWeightPresetUpdateRequest,
)
from app.services.campaign.campaign_service import CampaignService


def _make_service(preset_repo=None, audit_service=None):
    preset_repo = preset_repo or MagicMock()
    audit_service = audit_service or MagicMock()
    config_repo = MagicMock()
    # _validate_scoring_weights reads MIN_LAYER_WEIGHT for the individual
    # per-layer floor check - a bare MagicMock().get(...) isn't a real
    # dict/Decimal-convertible value, so this must be configured explicitly.
    config_repo.get_configs_by_keys.return_value = {"MIN_LAYER_WEIGHT": "5.00"}
    service = CampaignService(
        campaign_repo=MagicMock(),
        jd_repo=MagicMock(),
        audit_service=audit_service,
        config_repo=config_repo,
        preset_repo=preset_repo,
        db=MagicMock(),
    )
    return service, preset_repo, audit_service


def _create_request():
    return CampaignWeightPresetCreateRequest(
        name="Custom Preset",
        description="a custom preset",
        weight_deterministic=Decimal("30"),
        weight_semantic=Decimal("40"),
        weight_ai=Decimal("30"),
        deterministic_threshold=Decimal("70"),
        semantic_threshold=Decimal("65"),
        ai_threshold=Decimal("50"),
    )


def _update_request():
    return CampaignWeightPresetUpdateRequest(
        name="Renamed Preset",
        description="updated",
        weight_deterministic=Decimal("20"),
        weight_semantic=Decimal("50"),
        weight_ai=Decimal("30"),
        deterministic_threshold=Decimal("70"),
        semantic_threshold=Decimal("65"),
        ai_threshold=Decimal("50"),
    )


def _make_preset(org_id, **overrides):
    defaults = dict(
        id=uuid4(),
        org_id=org_id,
        name="Existing Preset",
        description=None,
        weight_deterministic=Decimal("30"),
        weight_semantic=Decimal("40"),
        weight_ai=Decimal("30"),
        deterministic_threshold=Decimal("70"),
        semantic_threshold=Decimal("65"),
        ai_threshold=Decimal("50"),
        created_by="hr-1",
        created_at=datetime.now(timezone.utc),
    )
    defaults.update(overrides)
    return CampaignWeightPreset(**defaults)


def _fake_flush_assigns_id(preset):
    """Simulates create()'s real db.add()+flush()+refresh(): the DB default
    populates id/created_at, which a MagicMock-backed repo never does on its own."""
    preset.id = uuid4()
    preset.created_at = datetime.now(timezone.utc)
    return preset


# ----------------------------------------------------------------------
# create_weight_preset
# ----------------------------------------------------------------------

def test_create_weight_preset_happy_path_commits_state_and_audit_together():
    preset_repo = MagicMock()
    preset_repo.get_by_name.return_value = None
    preset_repo.create.side_effect = _fake_flush_assigns_id
    audit_service = MagicMock()
    service, preset_repo, audit_service = _make_service(preset_repo, audit_service)

    result = service.create_weight_preset(_create_request(), org_id=uuid4(), created_by="hr-1")

    assert isinstance(result, CampaignWeightPresetResponse)
    audit_service.log.assert_called_once()
    assert audit_service.log.call_args.kwargs["action_type"] == "CAMPAIGN_WEIGHT_PRESET_CREATED"
    preset_repo.commit.assert_called_once()
    preset_repo.rollback.assert_not_called()
    # The old second, independent commit path must be gone entirely.
    audit_service.repository.save.assert_not_called()


def test_create_weight_preset_rolls_back_everything_when_audit_log_raises():
    preset_repo = MagicMock()
    preset_repo.get_by_name.return_value = None
    preset_repo.create.side_effect = _fake_flush_assigns_id
    audit_service = MagicMock()
    audit_service.log.side_effect = RuntimeError("audit write failed")
    service, preset_repo, audit_service = _make_service(preset_repo, audit_service)

    with pytest.raises(RuntimeError, match="audit write failed"):
        service.create_weight_preset(_create_request(), org_id=uuid4(), created_by="hr-1")

    preset_repo.commit.assert_not_called()
    preset_repo.rollback.assert_called_once()


# ----------------------------------------------------------------------
# update_weight_preset
# ----------------------------------------------------------------------

def test_update_weight_preset_happy_path_commits_state_and_audit_together():
    org_id = uuid4()
    existing = _make_preset(org_id)
    preset_repo = MagicMock()
    preset_repo.get_by_id.return_value = existing
    preset_repo.get_by_name.return_value = None
    preset_repo.update.side_effect = lambda p: p
    audit_service = MagicMock()
    service, preset_repo, audit_service = _make_service(preset_repo, audit_service)

    result = service.update_weight_preset(existing.id, _update_request(), org_id=org_id, updated_by="hr-1")

    assert isinstance(result, CampaignWeightPresetResponse)
    assert existing.name == "Renamed Preset"
    audit_service.log.assert_called_once()
    assert audit_service.log.call_args.kwargs["action_type"] == "CAMPAIGN_WEIGHT_PRESET_UPDATED"
    preset_repo.commit.assert_called_once()
    preset_repo.rollback.assert_not_called()
    audit_service.repository.save.assert_not_called()


def test_update_weight_preset_rolls_back_everything_when_audit_log_raises():
    org_id = uuid4()
    existing = _make_preset(org_id)
    preset_repo = MagicMock()
    preset_repo.get_by_id.return_value = existing
    preset_repo.get_by_name.return_value = None
    preset_repo.update.side_effect = lambda p: p
    audit_service = MagicMock()
    audit_service.log.side_effect = RuntimeError("audit write failed")
    service, preset_repo, audit_service = _make_service(preset_repo, audit_service)

    with pytest.raises(RuntimeError, match="audit write failed"):
        service.update_weight_preset(existing.id, _update_request(), org_id=org_id, updated_by="hr-1")

    preset_repo.commit.assert_not_called()
    preset_repo.rollback.assert_called_once()


# ----------------------------------------------------------------------
# delete_weight_preset
# ----------------------------------------------------------------------

def test_delete_weight_preset_happy_path_commits_state_and_audit_together():
    org_id = uuid4()
    existing = _make_preset(org_id)
    preset_repo = MagicMock()
    preset_repo.get_by_id.return_value = existing
    audit_service = MagicMock()
    service, preset_repo, audit_service = _make_service(preset_repo, audit_service)

    service.delete_weight_preset(existing.id, org_id=org_id, deleted_by="hr-1")

    preset_repo.delete.assert_called_once_with(existing)
    audit_service.log.assert_called_once()
    assert audit_service.log.call_args.kwargs["action_type"] == "CAMPAIGN_WEIGHT_PRESET_DELETED"
    # entity_id/details read off `existing` after delete()'s flush but
    # before commit() - the exact ordering that avoids the ObjectDeletedError
    # the old commit-then-read ordering was exposed to (expire_on_commit
    # defaults to True and nothing overrides it - app/db/session.py).
    assert audit_service.log.call_args.kwargs["entity_id"] == existing.id
    preset_repo.commit.assert_called_once()
    preset_repo.rollback.assert_not_called()
    audit_service.repository.save.assert_not_called()


def test_delete_weight_preset_rolls_back_everything_when_audit_log_raises():
    org_id = uuid4()
    existing = _make_preset(org_id)
    preset_repo = MagicMock()
    preset_repo.get_by_id.return_value = existing
    audit_service = MagicMock()
    audit_service.log.side_effect = RuntimeError("audit write failed")
    service, preset_repo, audit_service = _make_service(preset_repo, audit_service)

    with pytest.raises(RuntimeError, match="audit write failed"):
        service.delete_weight_preset(existing.id, org_id=org_id, deleted_by="hr-1")

    preset_repo.commit.assert_not_called()
    preset_repo.rollback.assert_called_once()
