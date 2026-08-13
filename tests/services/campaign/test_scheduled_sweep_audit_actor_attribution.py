"""
Epic 3 Fix 2: 4 scheduled/automated sweeps used to attribute their audit
entries to `campaign.created_by` (a real human, but one who did not trigger
this specific automated action) on the false premise that AuditLog.actor_id
is a required, non-null FK with no synthetic SYSTEM actor available.
actor_id is nullable (confirmed via the model and the live schema), and
StageTransitionService.transition() already logs SYSTEM-triggered writes
with actor_id=None/actor_role=SYSTEM - these 4 sites now do the same.

No prior test file touched any of these 4 methods, so this is first-ever
coverage, not a fixture update.
"""
from datetime import date
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

from app.models.campaigns import CampaignStatus
from app.services.campaign.campaign_scheduler_service import CampaignSchedulerService
from app.services.campaign.resubmission_alert_service import ResubmissionAlertService


def _make_campaign(**overrides):
    defaults = dict(
        id=uuid4(),
        name="Backend Engineer Q3",
        created_by="hr-1",
        status=CampaignStatus.ACTIVE,
        deadline=date(2026, 1, 1),
        max_candidates=5,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


# ----------------------------------------------------------------------
# CampaignSchedulerService.auto_close_expired_campaigns
# ----------------------------------------------------------------------

def test_auto_close_expired_campaigns_logs_system_not_the_campaign_creator():
    campaign = _make_campaign()
    campaign_repo = MagicMock()
    campaign_repo.get_expired_campaigns.side_effect = [[campaign], []]
    audit_service = MagicMock()
    service = CampaignSchedulerService(campaign_repo, audit_service, MagicMock())

    total_closed = service.auto_close_expired_campaigns()

    assert total_closed == 1
    campaign_repo.close_campaign.assert_called_once_with(campaign)
    audit_service.log.assert_called_once()
    assert audit_service.log.call_args.kwargs["actor_id"] is None
    assert audit_service.log.call_args.kwargs["actor_role"] == "SYSTEM"


# ----------------------------------------------------------------------
# CampaignSchedulerService.detect_stalled_candidate_alerts
# ----------------------------------------------------------------------

def test_detect_stalled_candidate_alerts_logs_system_not_the_campaign_creator():
    campaign = _make_campaign()
    campaign_repo = MagicMock()
    campaign_repo.get_all_campaigns.return_value = [campaign]
    campaign_repo.get_stalled_candidates.return_value = [
        {"pipeline_stage": "SCREENING", "stall_reason": "SLA_EXCEEDED"},
    ]
    config_repo = MagicMock()
    config_repo.get_configs_by_keys.return_value = {
        "SCREENING_SLA_HOURS": "48", "HM_REVIEW_SLA_DAYS": "5", "INTERVIEW_SLA_DAYS": "7",
    }
    audit_service = MagicMock()
    audit_service.get_latest_entry.return_value = None
    service = CampaignSchedulerService(campaign_repo, audit_service, config_repo)

    alerts_raised = service.detect_stalled_candidate_alerts()

    assert alerts_raised == 1
    audit_service.log.assert_called_once()
    assert audit_service.log.call_args.kwargs["actor_id"] is None
    assert audit_service.log.call_args.kwargs["actor_role"] == "SYSTEM"


# ----------------------------------------------------------------------
# CampaignSchedulerService._raise_health_alert
# ----------------------------------------------------------------------

def test_raise_health_alert_logs_system_not_the_campaign_creator():
    campaign = _make_campaign()
    audit_service = MagicMock()
    service = CampaignSchedulerService(MagicMock(), audit_service, MagicMock())

    service._raise_health_alert(
        campaign, condition="DEAD_TASK_COUNT_EXCEEDED", metric_detail={"dead_task_count": 12, "threshold": 10},
    )

    audit_service.log.assert_called_once()
    assert audit_service.log.call_args.kwargs["actor_id"] is None
    assert audit_service.log.call_args.kwargs["actor_role"] == "SYSTEM"


# ----------------------------------------------------------------------
# ResubmissionAlertService.evaluate_resubmission_alerts
# ----------------------------------------------------------------------

def test_evaluate_resubmission_alerts_logs_system_not_the_campaign_creator():
    candidate_id = uuid4()
    campaign = _make_campaign()
    campaign_candidate_repo = MagicMock()
    campaign_candidate_repo.get_high_frequency_resubmissions.return_value = [(candidate_id, 4)]
    campaign_candidate_repo.get_most_recent_campaign_for_candidate.return_value = campaign
    config_repo = MagicMock()
    config_repo.get_configs_by_keys.return_value = {
        "CROSS_CAMPAIGN_SUBMISSION_ALERT_THRESHOLD": "3", "CROSS_CAMPAIGN_SUBMISSION_WINDOW_DAYS": "30",
    }
    audit_service = MagicMock()
    service = ResubmissionAlertService(campaign_candidate_repo, audit_service, config_repo)

    alerts_raised = service.evaluate_resubmission_alerts()

    assert alerts_raised == 1
    audit_service.log.assert_called_once()
    assert audit_service.log.call_args.kwargs["actor_id"] is None
    assert audit_service.log.call_args.kwargs["actor_role"] == "SYSTEM"
    assert audit_service.log.call_args.kwargs["campaign_id"] == campaign.id
