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

    # Email plan phase 3: a draft (sent only after a preview and Send), recording who prepared it.
    assert result.status == "draft" and result.draft_id == notification.id
    mock_email_service.queue_rejection_email.assert_called_once_with(
        candidate_id=campaign_candidate.candidate_id, campaign_candidate_id=campaign_candidate.id, prepared_by="hm-1",
    )
    mock_task.apply_async.assert_not_called()


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

    assert result.status == "draft"


def test_send_rejection_email_never_bypasses_the_dedup():
    """Never twice (email plan, 2026-11-13): the manual send no longer passes allow_resend."""
    service, campaign_candidate, campaign, campaign_candidate_repo = _make_service()
    patcher, mock_email_service = _patched_email_service()

    with patcher, patch(f"{MODULE}.send_candidate_email_task"):
        service.send_rejection_email(campaign_candidate.id, actor_id="hm-1", actor_roles=["HIRING_MANAGER"])

    assert "allow_resend" not in mock_email_service.queue_rejection_email.call_args.kwargs


def test_send_rejection_email_reports_the_checks_refusal_and_queues_nothing():
    """A refusal by the checks before every email comes back with every problem (409 when already sent)."""
    from app.services.notifications import send_policy
    service, campaign_candidate, campaign, campaign_candidate_repo = _make_service()
    patcher, mock_email_service = _patched_email_service()

    with patcher, patch(f"{MODULE}.send_candidate_email_task") as mock_task, \
            patch.object(send_policy, "problems", return_value=[send_policy.Problem("ALREADY_SENT", "This email was already sent on 12 Nov 2026; it's never sent twice.")]):
        with pytest.raises(CampaignException) as exc_info:
            service.send_rejection_email(campaign_candidate.id, actor_id="hm-1", actor_roles=["HIRING_MANAGER"])

    assert exc_info.value.status_code == 409
    assert exc_info.value.data["problems"][0]["code"] == "ALREADY_SENT"
    mock_email_service.queue_rejection_email.assert_not_called()
    mock_task.apply_async.assert_not_called()


def test_send_rejection_email_raises_500_when_no_active_template():
    service, campaign_candidate, campaign, campaign_candidate_repo = _make_service()
    patcher, mock_email_service = _patched_email_service()
    template_repo = MagicMock()
    template_repo.get_active_by_trigger_event.return_value = None

    with patcher, patch(f"{MODULE}.send_candidate_email_task") as mock_task, \
            patch(f"{MODULE}.EmailTemplateRepository", return_value=template_repo):
        with pytest.raises(CampaignException) as exc_info:
            service.send_rejection_email(campaign_candidate.id, actor_id="hm-1", actor_roles=["HIRING_MANAGER"])

    assert exc_info.value.status_code == 500
    mock_email_service.queue_rejection_email.assert_not_called()
    mock_task.apply_async.assert_not_called()


def test_send_rejection_email_reports_a_lost_race_as_already_queued():
    service, campaign_candidate, campaign, campaign_candidate_repo = _make_service()
    patcher, mock_email_service = _patched_email_service()
    mock_email_service.queue_rejection_email.return_value = None

    with patcher, patch(f"{MODULE}.send_candidate_email_task") as mock_task:
        with pytest.raises(CampaignException) as exc_info:
            service.send_rejection_email(campaign_candidate.id, actor_id="hm-1", actor_roles=["HIRING_MANAGER"])

    assert exc_info.value.status_code == 409
    mock_task.apply_async.assert_not_called()
