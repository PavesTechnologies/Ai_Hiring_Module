"""
Bulk Add Candidates to Campaign (batched since 6436c7c): the campaign is checked
once up front (must be ACTIVE), existing members are looked up in one batch, a
resume is selected per candidate, every new campaign_candidates row (plus stage
history and audit) is inserted in ONE batch with ONE commit, and evaluation is
queued per added candidate. A unique-constraint conflict on the batch falls back
to the single-candidate add_candidate_to_campaign path for that batch. One
candidate's failure never affects another; failure reasons never leak internals.

Evaluation queueing (_queue_evaluation_tasks) is always patched here - no Celery
task is ever enqueued by these tests.
"""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError

from app.exception_handler.exceptions import UnprocessableError
from app.exceptions.campaign_exceptions import CampaignException
from app.models.campaigns import CampaignStatus
from app.models.pipeline import PipelineStage
from app.schemas.talent_pool.talent_pool_schema import AddCandidateToCampaignResponse
from app.services.talent_pool.talent_pool_service import TalentPoolService


def _selection(resume_id=None):
    return SimpleNamespace(selected_resume=SimpleNamespace(id=resume_id or uuid4()),
                           selection_method=MagicMock(value="MOST_RECENT"), evaluated_resumes=[])


def make_service(*, status=CampaignStatus.ACTIVE, existing=(), campaign=True):
    campaign_repo = MagicMock()
    campaign_repo.get_by_id_for_update.return_value = (
        SimpleNamespace(id=uuid4(), status=status) if campaign else None)
    campaign_candidate_repo = MagicMock()
    campaign_candidate_repo.get_existing_candidate_ids.return_value = set(existing)
    candidate_repo = MagicMock()
    candidate_repo.get_by_id.side_effect = lambda candidate_id: SimpleNamespace(id=candidate_id)
    selection_service = MagicMock()
    selection_service.select_resume_for_campaign.side_effect = lambda candidate_id, campaign: _selection()
    return TalentPoolService(
        candidate_repo=candidate_repo,
        resume_repo=MagicMock(),
        campaign_repo=campaign_repo,
        campaign_candidate_repo=campaign_candidate_repo,
        consent_repo=MagicMock(),
        encryption_service=MagicMock(),
        audit_service=MagicMock(),
        celery_task_log_service=MagicMock(),
        resume_selection_service=selection_service,
        skill_repo=MagicMock(),
    )


@pytest.fixture(autouse=True)
def no_task_queueing():
    with patch.object(TalentPoolService, "_queue_evaluation_tasks", return_value=[]) as queue:
        yield queue


def _bulk(service, candidate_ids, campaign_id=None):
    return service.bulk_add_candidates_to_campaign(candidate_ids, campaign_id or uuid4(),
                                                   actor_id="user-1", actor_role="HR_ADMIN")


def _inserted_rows(service):
    return [row for call in service.campaign_candidate_repo.bulk_create.call_args_list for row in call.args[0]]


def _added_response(candidate_id, campaign_id=None):
    return AddCandidateToCampaignResponse(
        campaign_candidate_id=uuid4(), campaign_id=campaign_id or uuid4(), candidate_id=candidate_id,
        resume_id=uuid4(), pipeline_stage=PipelineStage.UPLOADED, queued_task_types=[])


# --- the batch ----------------------------------------------------------------

def test_bulk_add_selects_a_resume_per_candidate_and_inserts_once():
    service = make_service()
    candidate_ids = [uuid4(), uuid4(), uuid4()]

    _bulk(service, candidate_ids)

    assert service.resume_selection_service.select_resume_for_campaign.call_count == 3
    service.campaign_candidate_repo.bulk_create.assert_called_once()
    rows = _inserted_rows(service)
    assert [row.candidate_id for row in rows] == candidate_ids
    assert all(row.pipeline_stage == PipelineStage.UPLOADED for row in rows)
    service.campaign_candidate_repo.bulk_create_stage_history.assert_called_once()
    service.campaign_candidate_repo.commit.assert_called_once()        # one commit for the whole batch
    assert service.audit_service.log.call_count == 3


def test_bulk_add_all_succeed():
    service = make_service()
    result = _bulk(service, [uuid4(), uuid4(), uuid4()])
    assert (result.total, result.added, result.failed) == (3, 3, 0)
    assert all(item.status == "ADDED" for item in result.results)


def test_bulk_add_all_fail():
    service = make_service()
    service.candidate_repo.get_by_id.side_effect = lambda candidate_id: None
    result = _bulk(service, [uuid4(), uuid4()])
    assert (result.added, result.failed) == (0, 2)
    assert all(item.status == "FAILED" for item in result.results)
    service.campaign_candidate_repo.bulk_create.assert_not_called()


def test_bulk_add_continues_after_one_candidate_fails():
    service = make_service()
    ok_a, broken, ok_b = uuid4(), uuid4(), uuid4()

    def select(candidate_id, campaign):
        if candidate_id == broken:
            raise UnprocessableError("No eligible resume for this candidate.")
        return _selection()
    service.resume_selection_service.select_resume_for_campaign.side_effect = select

    result = _bulk(service, [ok_a, broken, ok_b])

    assert (result.added, result.failed) == (2, 1)
    by_id = {item.candidate_id: item for item in result.results}
    assert by_id[broken].status == "FAILED" and by_id[broken].reason == "No eligible resume for this candidate."
    assert [row.candidate_id for row in _inserted_rows(service)] == [ok_a, ok_b]


def test_bulk_add_deduplicates_candidate_ids_preserving_order():
    service = make_service()
    a, b = uuid4(), uuid4()
    result = _bulk(service, [a, b, a, b, a])
    assert [item.candidate_id for item in result.results] == [a, b]
    assert service.resume_selection_service.select_resume_for_campaign.call_count == 2


def test_bulk_add_reports_candidate_not_found():
    service = make_service()
    missing = uuid4()
    service.candidate_repo.get_by_id.side_effect = lambda candidate_id: None
    result = _bulk(service, [missing])
    assert result.results[0].status == "FAILED"
    assert result.results[0].reason == f"Candidate {missing} not found."


def test_bulk_add_reports_already_in_campaign_from_one_batch_lookup():
    member, newcomer = uuid4(), uuid4()
    service = make_service(existing={member})
    result = _bulk(service, [member, newcomer])
    by_id = {item.candidate_id: item for item in result.results}
    assert by_id[member].status == "FAILED" and by_id[member].reason == "Candidate already exists in this campaign."
    assert by_id[newcomer].status == "ADDED"
    service.campaign_candidate_repo.get_existing_candidate_ids.assert_called_once()
    assert service.resume_selection_service.select_resume_for_campaign.call_count == 1   # never for the member


@pytest.mark.parametrize("campaign, status, code", [
    (False, CampaignStatus.ACTIVE, 404),
    (True, CampaignStatus.PAUSED, 409),
    (True, CampaignStatus.CLOSED, 403),
])
def test_an_unusable_campaign_fails_the_whole_request_up_front(campaign, status, code):
    service = make_service(campaign=campaign, status=status)
    with pytest.raises(CampaignException) as raised:
        _bulk(service, [uuid4(), uuid4()])
    assert raised.value.status_code == code
    service.resume_selection_service.select_resume_for_campaign.assert_not_called()
    service.campaign_candidate_repo.bulk_create.assert_not_called()


def test_bulk_add_different_candidates_can_select_different_resume_versions():
    service = make_service()
    a, b = uuid4(), uuid4()
    resume_a, resume_b = uuid4(), uuid4()
    service.resume_selection_service.select_resume_for_campaign.side_effect = (
        lambda candidate_id, campaign: _selection(resume_a if candidate_id == a else resume_b))
    result = _bulk(service, [a, b])
    assert {item.candidate_id: item.resume_id for item in result.results} == {a: resume_a, b: resume_b}


def test_bulk_add_catches_unexpected_errors_without_leaking_details():
    service = make_service()
    ok_id, broken_id = uuid4(), uuid4()

    def select(candidate_id, campaign):
        if candidate_id == broken_id:
            raise RuntimeError("division by zero at db_internal_module.py line 42")
        return _selection()
    service.resume_selection_service.select_resume_for_campaign.side_effect = select

    result = _bulk(service, [ok_id, broken_id])

    assert (result.added, result.failed) == (1, 1)
    failed = next(item for item in result.results if item.candidate_id == broken_id)
    assert "division by zero" not in failed.reason and "db_internal_module" not in failed.reason


def test_bulk_add_success_item_carries_the_new_row_and_resume():
    service = make_service()
    result = _bulk(service, [uuid4()])
    row = _inserted_rows(service)[0]
    item = result.results[0]
    assert (item.status, item.campaign_candidate_id, item.resume_id) == ("ADDED", row.id, row.resume_id)


def test_bulk_add_queues_evaluation_once_per_added_candidate_only(no_task_queueing):
    service = make_service()
    missing = uuid4()
    service.candidate_repo.get_by_id.side_effect = (
        lambda candidate_id: None if candidate_id == missing else SimpleNamespace(id=candidate_id))
    _bulk(service, [uuid4(), missing, uuid4()])
    assert no_task_queueing.call_count == 2


def test_bulk_add_does_not_roll_back_on_success():
    service = make_service()
    _bulk(service, [uuid4(), uuid4()])
    service.campaign_candidate_repo.rollback.assert_not_called()


def test_bulk_add_with_empty_candidate_list_returns_empty_results():
    service = make_service()
    result = _bulk(service, [])
    assert (result.total, result.added, result.failed, result.results) == (0, 0, 0, [])
    service.campaign_candidate_repo.bulk_create.assert_not_called()


# --- unique-constraint conflict: fall back to the single-candidate path ---------

def _conflicting_batch(service):
    service.campaign_candidate_repo.bulk_create.side_effect = IntegrityError("insert", {}, Exception("duplicate key"))


def test_a_conflicting_batch_rolls_back_and_falls_back_to_one_by_one():
    service = make_service()
    _conflicting_batch(service)
    a, b = uuid4(), uuid4()
    responses = {cid: _added_response(cid) for cid in (a, b)}

    with patch.object(service, "add_candidate_to_campaign",
                      side_effect=lambda cid, campaign_id, **_: responses[cid]) as single_add:
        result = _bulk(service, [a, b])

    service.campaign_candidate_repo.rollback.assert_called()
    assert single_add.call_count == 2
    by_id = {item.candidate_id: item for item in result.results}
    for cid in (a, b):
        assert by_id[cid].status == "ADDED"
        assert by_id[cid].campaign_candidate_id == responses[cid].campaign_candidate_id


def test_in_the_fallback_one_failed_candidate_is_rolled_back_alone():
    service = make_service()
    _conflicting_batch(service)
    ok_id, dup_id = uuid4(), uuid4()

    def single(cid, campaign_id, **_):
        if cid == dup_id:
            raise CampaignException("Candidate already exists in this campaign.", 409)
        return _added_response(cid, campaign_id)

    with patch.object(service, "add_candidate_to_campaign", side_effect=single):
        result = _bulk(service, [ok_id, dup_id])

    assert (result.added, result.failed) == (1, 1)
    # The conflicting batch is rolled back twice (inside _bulk_insert_campaign_candidates
    # and again by its caller - redundant but harmless), then the failed candidate once.
    assert service.campaign_candidate_repo.rollback.call_count == 3
    failed = next(item for item in result.results if item.candidate_id == dup_id)
    assert failed.reason == "Candidate already exists in this campaign."
