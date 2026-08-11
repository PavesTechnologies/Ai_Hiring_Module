from unittest.mock import MagicMock, patch
from uuid import uuid4

from app.exception_handler.exceptions import NotFoundError, UnprocessableError
from app.exceptions.campaign_exceptions import CampaignException
from app.models.pipeline import PipelineStage
from app.schemas.talent_pool.talent_pool_schema import AddCandidateToCampaignResponse
from app.services.talent_pool.talent_pool_service import TalentPoolService

"""
Bulk Add Candidates to Campaign - reuses add_candidate_to_campaign UNCHANGED,
looped once per (deduplicated) candidate with an independent outcome. These
tests mock add_candidate_to_campaign itself (a method on the same service
instance) rather than its full dependency graph, so they verify exactly the
thing this feature is about: that the existing single-candidate logic
(including its own ResumeSelectionService-backed selection, idempotency, and
audit logging) is reused per-candidate, not re-implemented, and that one
candidate's failure never affects any other.
"""


def make_service(campaign_candidate_repo=None):
    return TalentPoolService(
        candidate_repo=MagicMock(),
        resume_repo=MagicMock(),
        campaign_repo=MagicMock(),
        campaign_candidate_repo=campaign_candidate_repo or MagicMock(),
        consent_repo=MagicMock(),
        encryption_service=MagicMock(),
        audit_service=MagicMock(),
        celery_task_log_service=MagicMock(),
        resume_selection_service=MagicMock(),
        skill_repo=MagicMock(),
    )


def _success_response(**overrides):
    defaults = dict(
        campaign_candidate_id=uuid4(), campaign_id=uuid4(), candidate_id=uuid4(),
        resume_id=uuid4(), pipeline_stage=PipelineStage.UPLOADED, queued_task_types=[],
    )
    defaults.update(overrides)
    return AddCandidateToCampaignResponse(**defaults)


def test_bulk_add_calls_add_candidate_to_campaign_once_per_candidate():
    service = make_service()
    campaign_id = uuid4()
    candidate_ids = [uuid4(), uuid4(), uuid4()]

    with patch.object(service, "add_candidate_to_campaign", return_value=_success_response()) as mock_add:
        service.bulk_add_candidates_to_campaign(candidate_ids, campaign_id, actor_id="user-1", actor_role="HR_ADMIN")

    assert mock_add.call_count == 3
    for candidate_id in candidate_ids:
        mock_add.assert_any_call(candidate_id, campaign_id, actor_id="user-1", actor_role="HR_ADMIN")


def test_bulk_add_all_succeed():
    service = make_service()
    campaign_id = uuid4()
    candidate_ids = [uuid4(), uuid4()]
    responses = [_success_response(candidate_id=cid) for cid in candidate_ids]

    with patch.object(service, "add_candidate_to_campaign", side_effect=responses):
        result = service.bulk_add_candidates_to_campaign(candidate_ids, campaign_id, actor_id="user-1")

    assert result.campaign_id == campaign_id
    assert result.total == 2
    assert result.added == 2
    assert result.failed == 0
    assert all(item.status == "ADDED" for item in result.results)
    assert [item.candidate_id for item in result.results] == candidate_ids


def test_bulk_add_all_fail():
    service = make_service()
    candidate_ids = [uuid4(), uuid4(), uuid4()]

    with patch.object(
        service, "add_candidate_to_campaign",
        side_effect=UnprocessableError("Candidate has no eligible resume for campaign assignment."),
    ):
        result = service.bulk_add_candidates_to_campaign(candidate_ids, uuid4(), actor_id="user-1")

    assert result.total == 3
    assert result.added == 0
    assert result.failed == 3
    assert all(item.status == "FAILED" for item in result.results)
    assert all(item.reason == "Candidate has no eligible resume for campaign assignment." for item in result.results)


def test_bulk_add_continues_after_one_candidate_fails():
    service = make_service()
    ok_id, fail_id = uuid4(), uuid4()

    with patch.object(service, "add_candidate_to_campaign") as mock_add:
        mock_add.side_effect = [
            _success_response(candidate_id=ok_id),
            UnprocessableError("Candidate has no eligible resume for campaign assignment."),
        ]
        result = service.bulk_add_candidates_to_campaign([ok_id, fail_id], uuid4(), actor_id="user-1")

    assert result.total == 2
    assert result.added == 1
    assert result.failed == 1
    assert result.results[0].candidate_id == ok_id
    assert result.results[0].status == "ADDED"
    assert result.results[1].candidate_id == fail_id
    assert result.results[1].status == "FAILED"
    assert result.results[1].reason == "Candidate has no eligible resume for campaign assignment."
    # add_candidate_to_campaign was still attempted for both, despite the first failing.
    assert mock_add.call_count == 2


def test_bulk_add_deduplicates_candidate_ids_preserving_order():
    service = make_service()
    first, second = uuid4(), uuid4()

    with patch.object(service, "add_candidate_to_campaign") as mock_add:
        mock_add.side_effect = [_success_response(candidate_id=first), _success_response(candidate_id=second)]
        result = service.bulk_add_candidates_to_campaign([first, second, first], uuid4(), actor_id="user-1")

    assert mock_add.call_count == 2
    assert result.total == 2
    assert [item.candidate_id for item in result.results] == [first, second]


def test_bulk_add_reports_candidate_not_found():
    service = make_service()
    with patch.object(service, "add_candidate_to_campaign", side_effect=NotFoundError("Candidate x not found.")):
        result = service.bulk_add_candidates_to_campaign([uuid4()], uuid4(), actor_id="user-1")

    assert result.results[0].status == "FAILED"
    assert result.results[0].reason == "Candidate x not found."


def test_bulk_add_reports_already_in_campaign():
    service = make_service()
    with patch.object(
        service, "add_candidate_to_campaign",
        side_effect=CampaignException("Candidate already exists in this campaign.", 409),
    ):
        result = service.bulk_add_candidates_to_campaign([uuid4()], uuid4(), actor_id="user-1")

    assert result.results[0].status == "FAILED"
    assert result.results[0].reason == "Candidate already exists in this campaign."


def test_bulk_add_reports_campaign_validation_failure_per_candidate():
    """An invalid/paused/closed campaign surfaces as a per-candidate FAILED result, not a crashed request."""
    service = make_service()
    candidate_ids = [uuid4(), uuid4()]
    with patch.object(
        service, "add_candidate_to_campaign",
        side_effect=CampaignException("This campaign is closed and no longer accepting applications.", 403),
    ):
        result = service.bulk_add_candidates_to_campaign(candidate_ids, uuid4(), actor_id="user-1")

    assert result.added == 0
    assert result.failed == 2
    assert all(item.reason == "This campaign is closed and no longer accepting applications." for item in result.results)


def test_bulk_add_different_candidates_can_select_different_resume_versions():
    service = make_service()
    candidate_a, candidate_b = uuid4(), uuid4()
    resume_v2, resume_v4 = uuid4(), uuid4()

    with patch.object(service, "add_candidate_to_campaign") as mock_add:
        mock_add.side_effect = [
            _success_response(candidate_id=candidate_a, resume_id=resume_v2),
            _success_response(candidate_id=candidate_b, resume_id=resume_v4),
        ]
        result = service.bulk_add_candidates_to_campaign([candidate_a, candidate_b], uuid4(), actor_id="user-1")

    assert result.results[0].resume_id == resume_v2
    assert result.results[1].resume_id == resume_v4
    assert result.results[0].resume_id != result.results[1].resume_id


def test_bulk_add_passes_through_idempotent_retry_response_as_added():
    """add_candidate_to_campaign's idempotent-retry path returns a normal response - bulk reports it as ADDED."""
    service = make_service()
    existing_campaign_candidate_id = uuid4()
    existing_resume_id = uuid4()

    with patch.object(
        service, "add_candidate_to_campaign",
        return_value=_success_response(
            campaign_candidate_id=existing_campaign_candidate_id, resume_id=existing_resume_id, queued_task_types=[],
        ),
    ):
        result = service.bulk_add_candidates_to_campaign([uuid4()], uuid4(), actor_id="user-1")

    assert result.results[0].status == "ADDED"
    assert result.results[0].campaign_candidate_id == existing_campaign_candidate_id
    assert result.results[0].resume_id == existing_resume_id


def test_bulk_add_catches_unexpected_errors_without_leaking_details():
    """Even a totally unexpected exception must not crash the batch or leak internal details."""
    service = make_service()
    ok_id, broken_id = uuid4(), uuid4()

    with patch.object(service, "add_candidate_to_campaign") as mock_add:
        mock_add.side_effect = [
            _success_response(candidate_id=ok_id),
            RuntimeError("division by zero at db_internal_module.py line 42"),
        ]
        result = service.bulk_add_candidates_to_campaign([ok_id, broken_id], uuid4(), actor_id="user-1")

    assert result.added == 1
    assert result.failed == 1
    failed_item = result.results[1]
    assert failed_item.status == "FAILED"
    assert "division by zero" not in failed_item.reason
    assert "db_internal_module" not in failed_item.reason


def test_bulk_add_rolls_back_after_a_failed_candidate():
    """
    add_candidate_to_campaign doesn't roll back on every early-exit path
    (e.g. a FOR UPDATE lock acquired before the already-in-campaign check) -
    the bulk loop must roll back unconditionally on failure so it never
    bleeds lock/transaction state into the next candidate.
    """
    campaign_candidate_repo = MagicMock()
    service = make_service(campaign_candidate_repo=campaign_candidate_repo)

    with patch.object(
        service, "add_candidate_to_campaign",
        side_effect=CampaignException("Candidate already exists in this campaign.", 409),
    ):
        service.bulk_add_candidates_to_campaign([uuid4()], uuid4(), actor_id="user-1")

    campaign_candidate_repo.rollback.assert_called_once()


def test_bulk_add_does_not_roll_back_on_success():
    campaign_candidate_repo = MagicMock()
    service = make_service(campaign_candidate_repo=campaign_candidate_repo)

    with patch.object(service, "add_candidate_to_campaign", return_value=_success_response()):
        service.bulk_add_candidates_to_campaign([uuid4()], uuid4(), actor_id="user-1")

    campaign_candidate_repo.rollback.assert_not_called()


def test_bulk_add_success_item_carries_response_fields():
    service = make_service()
    response = _success_response()

    with patch.object(service, "add_candidate_to_campaign", return_value=response):
        result = service.bulk_add_candidates_to_campaign([response.candidate_id], uuid4(), actor_id="user-1")

    item = result.results[0]
    assert item.campaign_candidate_id == response.campaign_candidate_id
    assert item.resume_id == response.resume_id
    assert item.reason is None


def test_bulk_add_with_empty_candidate_list_returns_empty_results():
    service = make_service()
    campaign_id = uuid4()

    result = service.bulk_add_candidates_to_campaign([], campaign_id, actor_id="user-1")

    assert result.campaign_id == campaign_id
    assert result.results == []
    assert result.total == 0
    assert result.added == 0
    assert result.failed == 0
