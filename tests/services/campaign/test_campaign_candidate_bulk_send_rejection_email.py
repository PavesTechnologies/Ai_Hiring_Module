from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

from app.models.pipeline import PipelineStage
from app.services.campaign.campaign_candidate_service import CampaignCandidateService

MODULE = "app.services.campaign.campaign_candidate_service"

"""
Bulk follow-up to send_rejection_email (see
test_campaign_candidate_send_rejection_email.py) - calls that exact method
per id rather than duplicating its validation, so this file only covers
the bulk-specific concern: a per-id queued/failed split that never fails
the whole request over one bad id, unlike BulkStageMoveService.bulk_move's
all-or-nothing transition loop.
"""


def _make_campaign_candidate(pipeline_stage=PipelineStage.REJECTED, campaign_id=None):
    return SimpleNamespace(
        id=uuid4(), campaign_id=campaign_id or uuid4(), candidate_id=uuid4(), pipeline_stage=pipeline_stage,
    )


def _make_campaign(hiring_manager_id, campaign_id=None):
    return SimpleNamespace(id=campaign_id or uuid4(), hiring_manager_id=hiring_manager_id)


def _make_service(candidates_by_id, campaigns_by_id):
    campaign_candidate_repo = MagicMock()
    campaign_candidate_repo.get_by_id.side_effect = lambda cc_id: candidates_by_id.get(cc_id)

    campaign_repo = MagicMock()
    campaign_repo.get_by_id.side_effect = lambda campaign_id: campaigns_by_id.get(campaign_id)

    service = CampaignCandidateService(
        campaign_repo=campaign_repo, campaign_candidate_repo=campaign_candidate_repo, audit_service=MagicMock(),
    )
    return service


def _patched_collaborators():
    mock_email_service = MagicMock()
    mock_email_service.queue_rejection_email.return_value = SimpleNamespace(id=uuid4())
    return (
        patch(f"{MODULE}.CandidateRejectionEmailService", return_value=mock_email_service),
        patch(f"{MODULE}.send_candidate_email_task"),
        mock_email_service,
    )


def test_bulk_send_rejection_email_splits_valid_and_invalid_stage_candidates():
    rejected = _make_campaign_candidate(pipeline_stage=PipelineStage.REJECTED)
    screening = _make_campaign_candidate(pipeline_stage=PipelineStage.SCREENING)
    campaign = _make_campaign(hiring_manager_id="hm-1")
    for cc in (rejected, screening):
        cc.campaign_id = campaign.id

    service = _make_service({rejected.id: rejected, screening.id: screening}, {campaign.id: campaign})
    email_patcher, task_patcher, mock_email_service = _patched_collaborators()

    with email_patcher, task_patcher:
        result = service.bulk_send_rejection_email(
            [rejected.id, screening.id], actor_id="hm-1", actor_roles=["HIRING_MANAGER"],
        )

    assert result.queued == [rejected.id]
    assert len(result.failed) == 1
    assert result.failed[0]["campaign_candidate_id"] == str(screening.id)
    assert "hasn't been rejected" in result.failed[0]["reason"]
    assert mock_email_service.queue_rejection_email.call_count == 1


def test_bulk_send_rejection_email_excludes_candidate_outside_hiring_manager_campaign():
    owned_campaign = _make_campaign(hiring_manager_id="hm-1")
    other_campaign = _make_campaign(hiring_manager_id="someone-else")
    owned_candidate = _make_campaign_candidate(campaign_id=owned_campaign.id)
    outside_candidate = _make_campaign_candidate(campaign_id=other_campaign.id)

    service = _make_service(
        {owned_candidate.id: owned_candidate, outside_candidate.id: outside_candidate},
        {owned_campaign.id: owned_campaign, other_campaign.id: other_campaign},
    )
    email_patcher, task_patcher, mock_email_service = _patched_collaborators()

    with email_patcher, task_patcher:
        result = service.bulk_send_rejection_email(
            [owned_candidate.id, outside_candidate.id], actor_id="hm-1", actor_roles=["HIRING_MANAGER"],
        )

    assert result.queued == [owned_candidate.id]
    assert len(result.failed) == 1
    assert result.failed[0]["campaign_candidate_id"] == str(outside_candidate.id)
    assert mock_email_service.queue_rejection_email.call_count == 1


def test_bulk_send_rejection_email_hr_admin_processes_regardless_of_campaign_ownership():
    campaign_a = _make_campaign(hiring_manager_id="hm-a")
    campaign_b = _make_campaign(hiring_manager_id="hm-b")
    candidate_a = _make_campaign_candidate(campaign_id=campaign_a.id)
    candidate_b = _make_campaign_candidate(campaign_id=campaign_b.id)

    service = _make_service(
        {candidate_a.id: candidate_a, candidate_b.id: candidate_b},
        {campaign_a.id: campaign_a, campaign_b.id: campaign_b},
    )
    email_patcher, task_patcher, mock_email_service = _patched_collaborators()

    with email_patcher, task_patcher:
        result = service.bulk_send_rejection_email(
            [candidate_a.id, candidate_b.id], actor_id="hr-1", actor_roles=["HR_ADMIN"],
        )

    assert set(result.queued) == {candidate_a.id, candidate_b.id}
    assert result.failed == []
    assert mock_email_service.queue_rejection_email.call_count == 2


def test_bulk_send_rejection_email_allows_resend_matching_single_endpoint():
    rejected = _make_campaign_candidate()
    campaign = _make_campaign(hiring_manager_id="hm-1", campaign_id=rejected.campaign_id)
    service = _make_service({rejected.id: rejected}, {campaign.id: campaign})
    email_patcher, task_patcher, mock_email_service = _patched_collaborators()

    with email_patcher, task_patcher:
        service.bulk_send_rejection_email([rejected.id], actor_id="hm-1", actor_roles=["HIRING_MANAGER"])

    assert mock_email_service.queue_rejection_email.call_args.kwargs["allow_resend"] is True


def test_bulk_send_rejection_email_empty_list_is_a_clean_no_op():
    service = _make_service({}, {})
    email_patcher, task_patcher, mock_email_service = _patched_collaborators()

    with email_patcher, task_patcher:
        result = service.bulk_send_rejection_email([], actor_id="hr-1", actor_roles=["HR_ADMIN"])

    assert result.queued == []
    assert result.failed == []
    mock_email_service.queue_rejection_email.assert_not_called()


def test_bulk_send_rejection_email_not_found_id_is_reported_as_failed_not_raised():
    service = _make_service({}, {})
    email_patcher, task_patcher, mock_email_service = _patched_collaborators()
    missing_id = uuid4()

    with email_patcher, task_patcher:
        result = service.bulk_send_rejection_email([missing_id], actor_id="hr-1", actor_roles=["HR_ADMIN"])

    assert result.queued == []
    assert len(result.failed) == 1
    assert result.failed[0]["campaign_candidate_id"] == str(missing_id)
    mock_email_service.queue_rejection_email.assert_not_called()
