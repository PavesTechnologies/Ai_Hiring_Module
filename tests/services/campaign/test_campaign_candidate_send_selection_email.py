from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from app.exceptions.campaign_exceptions import CampaignException
from app.models.pipeline import PipelineStage
from app.services.campaign.campaign_candidate_service import CampaignCandidateService

MODULE = "app.services.campaign.campaign_candidate_service"

"""
M12 follow-up - Manual "Send Selection Email" action. Reaching SELECTED
(select-candidate, board drag-and-drop/bulk move, stalled-candidate
override) no longer queues this email automatically - mirrors
test_campaign_candidate_send_rejection_email.py exactly, just against
CANDIDATE_SELECTED/SELECTED instead of CANDIDATE_REJECTED/REJECTED.
"""


def _make_campaign_candidate(pipeline_stage=PipelineStage.SELECTED, campaign_id=None):
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


_NO_OVERRIDE = object()


def _patched_template_repo(template=_NO_OVERRIDE):
    mock_template_repo = MagicMock()
    mock_template_repo.get_active_by_trigger_event.return_value = (
        SimpleNamespace(id=uuid4()) if template is _NO_OVERRIDE else template
    )
    return patch(f"{MODULE}.EmailTemplateRepository", return_value=mock_template_repo), mock_template_repo


def test_send_selection_email_happy_path_queues():
    service, campaign_candidate, campaign, campaign_candidate_repo = _make_service()
    template_patcher, mock_template_repo = _patched_template_repo()

    with template_patcher, patch(f"{MODULE}.queue_candidate_selected_email") as mock_queue:
        result = service.send_selection_email(campaign_candidate.id, actor_id="hm-1", actor_roles=["HIRING_MANAGER"])

    assert result.status == "queued"
    mock_queue.assert_called_once_with(campaign_candidate_repo.db, campaign_candidate, allow_resend=True)


def test_send_selection_email_rejects_a_candidate_not_in_selected_stage():
    campaign_candidate = _make_campaign_candidate(pipeline_stage=PipelineStage.INTERVIEW)
    service, campaign_candidate, campaign, campaign_candidate_repo = _make_service(campaign_candidate=campaign_candidate)
    template_patcher, mock_template_repo = _patched_template_repo()

    with template_patcher, patch(f"{MODULE}.queue_candidate_selected_email") as mock_queue:
        with pytest.raises(CampaignException) as exc_info:
            service.send_selection_email(campaign_candidate.id, actor_id="hm-1", actor_roles=["HIRING_MANAGER"])

    assert exc_info.value.status_code == 400
    assert "hasn't been selected" in str(exc_info.value)
    mock_queue.assert_not_called()


def test_send_selection_email_raises_404_when_candidate_not_found():
    service, campaign_candidate, campaign, campaign_candidate_repo = _make_service()
    campaign_candidate_repo.get_by_id.return_value = None

    with pytest.raises(CampaignException) as exc_info:
        service.send_selection_email(uuid4(), actor_id="hm-1", actor_roles=["HIRING_MANAGER"])

    assert exc_info.value.status_code == 404


def test_send_selection_email_rejects_non_owning_hiring_manager():
    campaign = _make_campaign(hiring_manager_id="someone-else")
    service, campaign_candidate, campaign, campaign_candidate_repo = _make_service(campaign=campaign)

    with pytest.raises(CampaignException) as exc_info:
        service.send_selection_email(campaign_candidate.id, actor_id="hm-1", actor_roles=["HIRING_MANAGER"])

    assert exc_info.value.status_code == 403


def test_send_selection_email_allows_hr_admin_regardless_of_ownership():
    campaign = _make_campaign(hiring_manager_id="someone-else")
    service, campaign_candidate, campaign, campaign_candidate_repo = _make_service(campaign=campaign)
    template_patcher, mock_template_repo = _patched_template_repo()

    with template_patcher, patch(f"{MODULE}.queue_candidate_selected_email"):
        result = service.send_selection_email(campaign_candidate.id, actor_id="anyone", actor_roles=["HR_ADMIN"])

    assert result.status == "queued"


def test_send_selection_email_allows_resend():
    """The manual endpoint always passes allow_resend=True - queue_candidate_selected_email's own tests cover the bypass mechanics; this confirms the wiring."""
    service, campaign_candidate, campaign, campaign_candidate_repo = _make_service()
    template_patcher, mock_template_repo = _patched_template_repo()

    with template_patcher, patch(f"{MODULE}.queue_candidate_selected_email") as mock_queue:
        service.send_selection_email(campaign_candidate.id, actor_id="hm-1", actor_roles=["HIRING_MANAGER"])

    assert mock_queue.call_args.kwargs["allow_resend"] is True


def test_send_selection_email_raises_500_when_no_active_template():
    service, campaign_candidate, campaign, campaign_candidate_repo = _make_service()
    template_patcher, mock_template_repo = _patched_template_repo(template=None)

    with template_patcher, patch(f"{MODULE}.queue_candidate_selected_email") as mock_queue:
        with pytest.raises(CampaignException) as exc_info:
            service.send_selection_email(campaign_candidate.id, actor_id="hm-1", actor_roles=["HIRING_MANAGER"])

    assert exc_info.value.status_code == 500
    mock_queue.assert_not_called()
