from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from app.exceptions.campaign_exceptions import CampaignException
from app.models.pipeline import PipelineStage
from app.services.campaign.campaign_candidate_service import CampaignCandidateService

MODULE = "app.services.campaign.campaign_candidate_service"

"""
Manual "Send Rejection Email" action - human-driven rejection paths
(reject_at_interview, board drag-and-drop, bulk stage moves) never
auto-send, unlike the automated scoring paths. This method reuses
CandidateRejectionEmailService/the same CANDIDATE_REJECTED template the
automated path uses via ad-hoc construction (mocked here at the class
level, matching this session's established convention for testing
ad-hoc-constructed collaborators).
"""


def _make_campaign_candidate(pipeline_stage=PipelineStage.REJECTED, campaign_id=None):
    return SimpleNamespace(
        id=uuid4(), campaign_id=campaign_id or uuid4(), candidate_id=uuid4(), pipeline_stage=pipeline_stage,
    )


def _make_campaign(hiring_manager_id, campaign_id=None):
    return SimpleNamespace(id=campaign_id or uuid4(), hiring_manager_id=hiring_manager_id)


def _make_service(campaign_candidate=None, campaign=None):
    campaign_candidate = campaign_candidate or _make_campaign_candidate()
    campaign = campaign or _make_campaign(hiring_manager_id="hm-1", campaign_id=campaign_candidate.campaign_id)

    campaign_candidate_repo = MagicMock()
    campaign_candidate_repo.get_by_id.return_value = campaign_candidate

    campaign_repo = MagicMock()
    campaign_repo.get_by_id.return_value = campaign

    service = CampaignCandidateService(
        campaign_repo=campaign_repo, campaign_candidate_repo=campaign_candidate_repo, audit_service=MagicMock(),
    )
    return service, campaign_candidate, campaign, campaign_candidate_repo


def _patched_email_service(notification=None):
    mock_email_service = MagicMock()
    mock_email_service.queue_rejection_email.return_value = notification or SimpleNamespace(id=uuid4())
    return patch(f"{MODULE}.CandidateRejectionEmailService", return_value=mock_email_service), mock_email_service


def test_send_rejection_email_happy_path_queues_and_dispatches():
    service, campaign_candidate, campaign, campaign_candidate_repo = _make_service()
    notification = SimpleNamespace(id=uuid4())
    patcher, mock_email_service = _patched_email_service(notification)

    with patcher, patch(f"{MODULE}.send_candidate_email_task") as mock_task:
        result = service.send_rejection_email(campaign_candidate.id, actor_id="hm-1", actor_roles=["HIRING_MANAGER"])

    assert result.status == "queued"
    mock_email_service.queue_rejection_email.assert_called_once_with(
        candidate_id=campaign_candidate.candidate_id, campaign_candidate_id=campaign_candidate.id, allow_resend=True,
    )
    mock_task.apply_async.assert_called_once_with(kwargs={"email_notification_id": str(notification.id)})


def test_send_rejection_email_rejects_a_candidate_not_in_rejected_stage():
    campaign_candidate = _make_campaign_candidate(pipeline_stage=PipelineStage.SCREENING)
    service, campaign_candidate, campaign, campaign_candidate_repo = _make_service(campaign_candidate=campaign_candidate)
    patcher, mock_email_service = _patched_email_service()

    with patcher, patch(f"{MODULE}.send_candidate_email_task") as mock_task:
        with pytest.raises(CampaignException) as exc_info:
            service.send_rejection_email(campaign_candidate.id, actor_id="hm-1", actor_roles=["HIRING_MANAGER"])

    assert exc_info.value.status_code == 400
    assert "hasn't been rejected" in str(exc_info.value)
    mock_email_service.queue_rejection_email.assert_not_called()
    mock_task.apply_async.assert_not_called()


def test_send_rejection_email_raises_404_when_candidate_not_found():
    service, campaign_candidate, campaign, campaign_candidate_repo = _make_service()
    campaign_candidate_repo.get_by_id.return_value = None

    with pytest.raises(CampaignException) as exc_info:
        service.send_rejection_email(uuid4(), actor_id="hm-1", actor_roles=["HIRING_MANAGER"])

    assert exc_info.value.status_code == 404


def test_send_rejection_email_rejects_non_owning_hiring_manager():
    campaign = _make_campaign(hiring_manager_id="someone-else")
    service, campaign_candidate, campaign, campaign_candidate_repo = _make_service(campaign=campaign)

    with pytest.raises(CampaignException) as exc_info:
        service.send_rejection_email(campaign_candidate.id, actor_id="hm-1", actor_roles=["HIRING_MANAGER"])

    assert exc_info.value.status_code == 403


def test_send_rejection_email_allows_hr_admin_regardless_of_ownership():
    campaign = _make_campaign(hiring_manager_id="someone-else")
    service, campaign_candidate, campaign, campaign_candidate_repo = _make_service(campaign=campaign)
    patcher, mock_email_service = _patched_email_service()

    with patcher, patch(f"{MODULE}.send_candidate_email_task"):
        result = service.send_rejection_email(campaign_candidate.id, actor_id="anyone", actor_roles=["HR_ADMIN"])

    assert result.status == "queued"


def test_send_rejection_email_allows_resend_of_an_already_sent_email():
    """The manual endpoint always passes allow_resend=True - CandidateRejectionEmailService's own tests cover the bypass mechanics; this confirms the wiring."""
    service, campaign_candidate, campaign, campaign_candidate_repo = _make_service()
    patcher, mock_email_service = _patched_email_service()

    with patcher, patch(f"{MODULE}.send_candidate_email_task"):
        service.send_rejection_email(campaign_candidate.id, actor_id="hm-1", actor_roles=["HIRING_MANAGER"])

    assert mock_email_service.queue_rejection_email.call_args.kwargs["allow_resend"] is True


def test_send_rejection_email_raises_500_when_no_active_template():
    service, campaign_candidate, campaign, campaign_candidate_repo = _make_service()
    patcher, mock_email_service = _patched_email_service()
    mock_email_service.queue_rejection_email.return_value = None

    with patcher, patch(f"{MODULE}.send_candidate_email_task") as mock_task:
        with pytest.raises(CampaignException) as exc_info:
            service.send_rejection_email(campaign_candidate.id, actor_id="hm-1", actor_roles=["HIRING_MANAGER"])

    assert exc_info.value.status_code == 500
    mock_task.apply_async.assert_not_called()
