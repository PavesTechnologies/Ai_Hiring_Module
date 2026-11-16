from contextlib import nullcontext
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
    """
    Email plan phase 3: after its confirm step (the check), the bulk send queues each email through
    drafts.queue_confirmed - exposed here as queue_rejection_email so each test reads as before.
    """
    queue_confirmed = MagicMock(return_value=SimpleNamespace(id=uuid4()))
    return (
        patch("app.services.notifications.drafts.queue_confirmed", queue_confirmed),
        nullcontext(),
        SimpleNamespace(queue_rejection_email=queue_confirmed),
    )


def _send_after_check(service, ids, actor_id, actor_roles):
    """The confirm step first (who will receive it), then the send with its check_hash."""
    check = service.check_bulk_rejection_email(ids, actor_id=actor_id, actor_roles=actor_roles)
    return service.bulk_send_rejection_email(ids, actor_id=actor_id, actor_roles=actor_roles,
                                             check_hash=check.check_hash)


def test_bulk_send_rejection_email_refuses_without_a_matching_check():
    import pytest
    from app.exceptions.campaign_exceptions import CampaignException
    rejected = _make_campaign_candidate()
    campaign = _make_campaign(hiring_manager_id="hm-1", campaign_id=rejected.campaign_id)
    service = _make_service({rejected.id: rejected}, {campaign.id: campaign})
    email_patcher, task_patcher, mock_email_service = _patched_collaborators()

    with email_patcher, task_patcher, pytest.raises(CampaignException) as refused:
        service.bulk_send_rejection_email([rejected.id], actor_id="hm-1", actor_roles=["HIRING_MANAGER"],
                                          check_hash="stale")

    assert refused.value.status_code == 409 and refused.value.data["code"] == "CHECK_CHANGED"
    mock_email_service.queue_rejection_email.assert_not_called()


def test_bulk_send_rejection_email_splits_valid_and_invalid_stage_candidates():
    rejected = _make_campaign_candidate(pipeline_stage=PipelineStage.REJECTED)
    screening = _make_campaign_candidate(pipeline_stage=PipelineStage.SCREENING)
    campaign = _make_campaign(hiring_manager_id="hm-1")
    for cc in (rejected, screening):
        cc.campaign_id = campaign.id

    service = _make_service({rejected.id: rejected, screening.id: screening}, {campaign.id: campaign})
    email_patcher, task_patcher, mock_email_service = _patched_collaborators()

    with email_patcher, task_patcher:
        result = _send_after_check(
            service,             [rejected.id, screening.id], actor_id="hm-1", actor_roles=["HIRING_MANAGER"],
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
        result = _send_after_check(
            service,             [owned_candidate.id, outside_candidate.id], actor_id="hm-1", actor_roles=["HIRING_MANAGER"],
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
        result = _send_after_check(
            service,             [candidate_a.id, candidate_b.id], actor_id="hr-1", actor_roles=["HR_ADMIN"],
        )

    assert set(result.queued) == {candidate_a.id, candidate_b.id}
    assert result.failed == []
    assert mock_email_service.queue_rejection_email.call_count == 2


def test_bulk_send_rejection_email_never_bypasses_the_dedup_matching_single_endpoint():
    rejected = _make_campaign_candidate()
    campaign = _make_campaign(hiring_manager_id="hm-1", campaign_id=rejected.campaign_id)
    service = _make_service({rejected.id: rejected}, {campaign.id: campaign})
    email_patcher, task_patcher, mock_email_service = _patched_collaborators()

    with email_patcher, task_patcher:
        _send_after_check(service, [rejected.id], actor_id="hm-1", actor_roles=["HIRING_MANAGER"])

    assert "allow_resend" not in mock_email_service.queue_rejection_email.call_args.kwargs


def test_bulk_send_rejection_email_sends_once_to_a_repeated_candidate():
    rejected = _make_campaign_candidate()
    other = _make_campaign_candidate()
    campaign = _make_campaign(hiring_manager_id="hm-1", campaign_id=rejected.campaign_id)
    other_campaign = _make_campaign(hiring_manager_id="hm-1", campaign_id=other.campaign_id)
    service = _make_service({rejected.id: rejected, other.id: other},
                            {campaign.id: campaign, other_campaign.id: other_campaign})
    email_patcher, task_patcher, mock_email_service = _patched_collaborators()

    with email_patcher, task_patcher:
        result = _send_after_check(service, [rejected.id, rejected.id, other.id], actor_id="hm-1",
                                                   actor_roles=["HIRING_MANAGER"])

    assert result.queued == [rejected.id, other.id] and result.repeated == 1
    assert mock_email_service.queue_rejection_email.call_count == 2
    assert "1 repeated" in result.detail


def test_bulk_send_rejection_email_reports_the_problem_codes():
    from app.services.notifications import send_policy
    rejected = _make_campaign_candidate()
    campaign = _make_campaign(hiring_manager_id="hm-1", campaign_id=rejected.campaign_id)
    service = _make_service({rejected.id: rejected}, {campaign.id: campaign})
    email_patcher, task_patcher, mock_email_service = _patched_collaborators()
    erased = send_policy.Problem("CANDIDATE_ERASED", "The candidate's data has been erased.")

    with email_patcher, task_patcher, patch.object(send_policy, "problems", return_value=[erased]):
        result = _send_after_check(service, [rejected.id], actor_id="hm-1", actor_roles=["HIRING_MANAGER"])

    assert result.queued == [] and result.failed[0]["codes"] == ["CANDIDATE_ERASED"]
    mock_email_service.queue_rejection_email.assert_not_called()


def test_bulk_send_rejection_email_empty_list_is_a_clean_no_op():
    service = _make_service({}, {})
    email_patcher, task_patcher, mock_email_service = _patched_collaborators()

    with email_patcher, task_patcher:
        result = _send_after_check(service, [], actor_id="hr-1", actor_roles=["HR_ADMIN"])

    assert result.queued == []
    assert result.failed == []
    mock_email_service.queue_rejection_email.assert_not_called()


def test_bulk_send_rejection_email_not_found_id_is_reported_as_failed_not_raised():
    service = _make_service({}, {})
    email_patcher, task_patcher, mock_email_service = _patched_collaborators()
    missing_id = uuid4()

    with email_patcher, task_patcher:
        result = _send_after_check(service, [missing_id], actor_id="hr-1", actor_roles=["HR_ADMIN"])

    assert result.queued == []
    assert len(result.failed) == 1
    assert result.failed[0]["campaign_candidate_id"] == str(missing_id)
    mock_email_service.queue_rejection_email.assert_not_called()
