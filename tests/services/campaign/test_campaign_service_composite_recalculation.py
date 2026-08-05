from unittest.mock import MagicMock, patch
from uuid import uuid4

from app.models.pipeline import CompositeScoreTriggerSource
from app.services.campaign.campaign_service import CampaignService

SERVICE_MODULE = "app.services.campaign.campaign_service"


def _make_service():
    return CampaignService(
        campaign_repo=MagicMock(),
        jd_repo=MagicMock(),
        audit_service=MagicMock(),
        config_repo=MagicMock(),
        preset_repo=MagicMock(),
        db=MagicMock(),
        circuit_breaker_repo=MagicMock(),
        dead_letter_queue_repo=MagicMock(),
        prompt_template_repo=MagicMock(),
    )


def test_enqueue_composite_recalculation_fans_out_to_every_candidate_in_campaign():
    """
    M10-E01 Design Decision 9: a campaign weight change recalculates ONLY
    composite_score - for EVERY existing candidate in that campaign, not
    just one.
    """
    campaign_id = uuid4()
    candidate_ids = [uuid4(), uuid4(), uuid4()]

    with patch(f"{SERVICE_MODULE}.CampaignCandidateRepository") as repo_cls, \
         patch(f"{SERVICE_MODULE}.CeleryTaskLogRepository"), \
         patch(f"{SERVICE_MODULE}.CeleryTaskLogService") as task_log_service_cls, \
         patch(f"{SERVICE_MODULE}._enqueue_composite_scoring") as enqueue_mock:
        repo_cls.return_value.get_ids_by_campaign.return_value = candidate_ids
        task_log_service_instance = MagicMock()
        task_log_service_cls.return_value = task_log_service_instance

        service = _make_service()
        service._enqueue_composite_recalculation_for_campaign(campaign_id)

        repo_cls.return_value.get_ids_by_campaign.assert_called_once_with(campaign_id)
        assert enqueue_mock.call_count == len(candidate_ids)
        for call, candidate_id in zip(enqueue_mock.call_args_list, candidate_ids):
            assert call.args == (candidate_id, task_log_service_instance, CompositeScoreTriggerSource.CAMPAIGN_WEIGHT_CHANGE)


def test_enqueue_composite_recalculation_swallows_failures():
    """Best-effort - must never raise, same convention as _queue_post_override_evaluation."""
    with patch(f"{SERVICE_MODULE}.CampaignCandidateRepository") as repo_cls:
        repo_cls.return_value.get_ids_by_campaign.side_effect = Exception("db unreachable")

        service = _make_service()
        # Must not raise.
        service._enqueue_composite_recalculation_for_campaign(uuid4())


def test_update_scoring_configuration_triggers_recalculation_only_when_weights_change():
    """
    A thresholds-only change (no weight_deterministic/semantic/ai in the
    diff) must NOT trigger composite recalculation - only an actual weight
    change does.
    """
    service = _make_service()
    service._enqueue_composite_recalculation_for_campaign = MagicMock()

    weights_changed = {"weight_deterministic": {"before": "30.00", "after": "40.00"}}
    thresholds_only = {"semantic_threshold": {"before": "0.6500", "after": "0.7000"}}

    from app.services.campaign.campaign_service import _WEIGHT_FIELDS

    assert bool(_WEIGHT_FIELDS & weights_changed.keys()) is True
    assert bool(_WEIGHT_FIELDS & thresholds_only.keys()) is False
