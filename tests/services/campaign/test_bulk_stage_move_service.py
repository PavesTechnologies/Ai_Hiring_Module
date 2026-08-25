from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from app.exceptions.campaign_exceptions import CampaignException
from app.models.pipeline import PipelineStage
from app.services.campaign.bulk_stage_move_service import BulkStageMoveService

"""
M11-E04-S03 — zero test coverage existed for this class before Epic 5
Step 2 (confirmed via repo-wide grep for bulk_move/move_one/reject_one
across tests/ - no hits). Written now because bulk_move/move_one are 2
of PipelineTransitionService's 3 real callers that need the
CANDIDATE_SELECTED email hook independently (transition_stage() itself
never commits, so the hook can't live there - see
candidate_notification_emails.py's own docstring) - this file covers
both the pre-existing orchestration and the new hook together, rather
than adding the hook to code with no safety net at all.
"""


def _cc(campaign_id, pipeline_stage=PipelineStage.INTERVIEW, **overrides):
    defaults = dict(id=uuid4(), campaign_id=campaign_id, candidate_id=uuid4(), pipeline_stage=pipeline_stage)
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def make_service(**overrides):
    defaults = dict(
        campaign_candidate_repo=MagicMock(), pipeline_transition_service=MagicMock(), audit_service=MagicMock(),
    )
    defaults.update(overrides)
    return BulkStageMoveService(**defaults)


# ----------------------------------------------------------------------
# bulk_move
# ----------------------------------------------------------------------

def test_bulk_move_transitions_every_candidate_and_commits_once():
    campaign_id = uuid4()
    candidates = [_cc(campaign_id), _cc(campaign_id)]
    campaign_candidate_repo = MagicMock()
    campaign_candidate_repo.get_by_id.side_effect = candidates
    service = make_service(campaign_candidate_repo=campaign_candidate_repo)

    result = service.bulk_move(
        campaign_id=campaign_id, campaign_candidate_ids=[c.id for c in candidates],
        target_stage="SHORTLISTED", reason="promoted after panel review", actor_id="hm-1", actor_role="HIRING_MANAGER",
    )

    assert service.pipeline_transition_service.transition_stage.call_count == 2
    campaign_candidate_repo.commit.assert_called_once()
    assert result.moved_count == 2


def test_bulk_move_rejects_a_mixed_source_stage_selection():
    campaign_id = uuid4()
    candidates = [_cc(campaign_id, pipeline_stage=PipelineStage.INTERVIEW), _cc(campaign_id, pipeline_stage=PipelineStage.SCREENING)]
    campaign_candidate_repo = MagicMock()
    campaign_candidate_repo.get_by_id.side_effect = candidates
    service = make_service(campaign_candidate_repo=campaign_candidate_repo)

    with pytest.raises(CampaignException) as exc_info:
        service.bulk_move(
            campaign_id=campaign_id, campaign_candidate_ids=[c.id for c in candidates],
            target_stage="SELECTED", reason="promoted after panel review", actor_id="hm-1", actor_role="HIRING_MANAGER",
        )

    assert exc_info.value.status_code == 409


def test_bulk_move_rolls_back_on_a_failed_transition():
    campaign_id = uuid4()
    candidates = [_cc(campaign_id)]
    campaign_candidate_repo = MagicMock()
    campaign_candidate_repo.get_by_id.side_effect = candidates
    pipeline_transition_service = MagicMock()
    pipeline_transition_service.transition_stage.side_effect = RuntimeError("boom")
    service = make_service(campaign_candidate_repo=campaign_candidate_repo, pipeline_transition_service=pipeline_transition_service)

    with pytest.raises(RuntimeError):
        service.bulk_move(
            campaign_id=campaign_id, campaign_candidate_ids=[c.id for c in candidates],
            target_stage="SHORTLISTED", reason="promoted after panel review", actor_id="hm-1", actor_role="HIRING_MANAGER",
        )

    campaign_candidate_repo.rollback.assert_called_once()
    campaign_candidate_repo.commit.assert_not_called()


def test_bulk_move_queues_a_selected_email_per_candidate_when_target_is_selected():
    campaign_id = uuid4()
    candidates = [_cc(campaign_id), _cc(campaign_id)]
    campaign_candidate_repo = MagicMock()
    campaign_candidate_repo.get_by_id.side_effect = candidates
    service = make_service(campaign_candidate_repo=campaign_candidate_repo)

    with patch("app.services.campaign.bulk_stage_move_service.queue_candidate_selected_email") as mock_queue:
        service.bulk_move(
            campaign_id=campaign_id, campaign_candidate_ids=[c.id for c in candidates],
            target_stage="SELECTED", reason="both cleared final round", actor_id="hm-1", actor_role="HIRING_MANAGER",
        )

    assert mock_queue.call_count == 2
    mock_queue.assert_any_call(campaign_candidate_repo.db, candidates[0])
    mock_queue.assert_any_call(campaign_candidate_repo.db, candidates[1])


def test_bulk_move_does_not_queue_selected_email_for_other_target_stages():
    campaign_id = uuid4()
    candidates = [_cc(campaign_id)]
    campaign_candidate_repo = MagicMock()
    campaign_candidate_repo.get_by_id.side_effect = candidates
    service = make_service(campaign_candidate_repo=campaign_candidate_repo)

    with patch("app.services.campaign.bulk_stage_move_service.queue_candidate_selected_email") as mock_queue:
        service.bulk_move(
            campaign_id=campaign_id, campaign_candidate_ids=[c.id for c in candidates],
            target_stage="SHORTLISTED", reason="promoted after panel review", actor_id="hm-1", actor_role="HIRING_MANAGER",
        )

    mock_queue.assert_not_called()


def test_bulk_move_enqueues_rescore_per_candidate_when_target_is_screening_from_hold():
    campaign_id = uuid4()
    candidates = [_cc(campaign_id, pipeline_stage=PipelineStage.HOLD), _cc(campaign_id, pipeline_stage=PipelineStage.HOLD)]
    campaign_candidate_repo = MagicMock()
    campaign_candidate_repo.get_by_id.side_effect = candidates
    service = make_service(campaign_candidate_repo=campaign_candidate_repo)

    with patch("app.services.campaign.bulk_stage_move_service.enqueue_manual_rescore") as mock_rescore:
        service.bulk_move(
            campaign_id=campaign_id, campaign_candidate_ids=[c.id for c in candidates],
            target_stage="SCREENING", reason="returned for re-evaluation", actor_id="hm-1", actor_role="HIRING_MANAGER",
        )

    assert mock_rescore.call_count == 2
    mock_rescore.assert_any_call(campaign_candidate_repo.db, candidates[0])
    mock_rescore.assert_any_call(campaign_candidate_repo.db, candidates[1])


def test_bulk_move_never_enqueues_rescore_from_uploaded():
    campaign_id = uuid4()
    candidates = [_cc(campaign_id, pipeline_stage=PipelineStage.UPLOADED)]
    campaign_candidate_repo = MagicMock()
    campaign_candidate_repo.get_by_id.side_effect = candidates
    service = make_service(campaign_candidate_repo=campaign_candidate_repo)

    with patch("app.services.campaign.bulk_stage_move_service.enqueue_manual_rescore") as mock_rescore:
        service.bulk_move(
            campaign_id=campaign_id, campaign_candidate_ids=[c.id for c in candidates],
            target_stage="SCREENING", reason="returned for re-evaluation", actor_id="hm-1", actor_role="HIRING_MANAGER",
        )

    mock_rescore.assert_not_called()


# ----------------------------------------------------------------------
# move_one
# ----------------------------------------------------------------------

def test_move_one_transitions_and_commits():
    campaign_id = uuid4()
    cc = _cc(campaign_id)
    campaign_candidate_repo = MagicMock()
    campaign_candidate_repo.get_by_id.return_value = cc
    service = make_service(campaign_candidate_repo=campaign_candidate_repo)

    result = service.move_one(
        campaign_id=campaign_id, campaign_candidate_id=cc.id, target_stage="SHORTLISTED",
        reason="promoted after panel review", actor_id="hm-1", actor_role="HIRING_MANAGER",
    )

    service.pipeline_transition_service.transition_stage.assert_called_once()
    campaign_candidate_repo.commit.assert_called_once()
    assert result.campaign_candidate_id == cc.id


def test_move_one_queues_selected_email_after_commit_when_target_is_selected():
    campaign_id = uuid4()
    cc = _cc(campaign_id)
    campaign_candidate_repo = MagicMock()
    campaign_candidate_repo.get_by_id.return_value = cc
    service = make_service(campaign_candidate_repo=campaign_candidate_repo)

    with patch("app.services.campaign.bulk_stage_move_service.queue_candidate_selected_email") as mock_queue:
        service.move_one(
            campaign_id=campaign_id, campaign_candidate_id=cc.id, target_stage="SELECTED",
            reason="cleared final round", actor_id="hm-1", actor_role="HIRING_MANAGER",
        )

    mock_queue.assert_called_once_with(campaign_candidate_repo.db, cc)


def test_move_one_does_not_queue_selected_email_for_other_target_stages():
    campaign_id = uuid4()
    cc = _cc(campaign_id)
    campaign_candidate_repo = MagicMock()
    campaign_candidate_repo.get_by_id.return_value = cc
    service = make_service(campaign_candidate_repo=campaign_candidate_repo)

    with patch("app.services.campaign.bulk_stage_move_service.queue_candidate_selected_email") as mock_queue:
        service.move_one(
            campaign_id=campaign_id, campaign_candidate_id=cc.id, target_stage="SHORTLISTED",
            reason="promoted after panel review", actor_id="hm-1", actor_role="HIRING_MANAGER",
        )

    mock_queue.assert_not_called()


def test_move_one_enqueues_rescore_after_commit_when_target_is_screening_from_fraud_review():
    campaign_id = uuid4()
    cc = _cc(campaign_id, pipeline_stage=PipelineStage.FRAUD_REVIEW)
    campaign_candidate_repo = MagicMock()
    campaign_candidate_repo.get_by_id.return_value = cc
    service = make_service(campaign_candidate_repo=campaign_candidate_repo)

    with patch("app.services.campaign.bulk_stage_move_service.enqueue_manual_rescore") as mock_rescore:
        service.move_one(
            campaign_id=campaign_id, campaign_candidate_id=cc.id, target_stage="SCREENING",
            reason="returned for re-evaluation", actor_id="hm-1", actor_role="HIRING_MANAGER",
        )

    mock_rescore.assert_called_once_with(campaign_candidate_repo.db, cc)


def test_move_one_never_enqueues_rescore_from_uploaded():
    campaign_id = uuid4()
    cc = _cc(campaign_id, pipeline_stage=PipelineStage.UPLOADED)
    campaign_candidate_repo = MagicMock()
    campaign_candidate_repo.get_by_id.return_value = cc
    service = make_service(campaign_candidate_repo=campaign_candidate_repo)

    with patch("app.services.campaign.bulk_stage_move_service.enqueue_manual_rescore") as mock_rescore:
        service.move_one(
            campaign_id=campaign_id, campaign_candidate_id=cc.id, target_stage="SCREENING",
            reason="returned for re-evaluation", actor_id="hm-1", actor_role="HIRING_MANAGER",
        )

    mock_rescore.assert_not_called()


def test_move_one_raises_404_for_candidate_outside_the_campaign():
    campaign_candidate_repo = MagicMock()
    campaign_candidate_repo.get_by_id.return_value = None
    service = make_service(campaign_candidate_repo=campaign_candidate_repo)

    with pytest.raises(CampaignException) as exc_info:
        service.move_one(
            campaign_id=uuid4(), campaign_candidate_id=uuid4(), target_stage="SHORTLISTED",
            reason="promoted after panel review", actor_id="hm-1", actor_role="HIRING_MANAGER",
        )

    assert exc_info.value.status_code == 404


# ----------------------------------------------------------------------
# reject_one - a thin wrapper over move_one, never reaches SELECTED.
# ----------------------------------------------------------------------

def test_reject_one_never_queues_a_selected_email():
    campaign_id = uuid4()
    cc = _cc(campaign_id, pipeline_stage=PipelineStage.SCREENING)
    campaign_candidate_repo = MagicMock()
    campaign_candidate_repo.get_by_id.return_value = cc
    service = make_service(campaign_candidate_repo=campaign_candidate_repo)

    with patch("app.services.campaign.bulk_stage_move_service.queue_candidate_selected_email") as mock_queue:
        service.reject_one(campaign_id=campaign_id, campaign_candidate_id=cc.id, reason="did not meet the bar", actor_id="hm-1", actor_role="HIRING_MANAGER")

    mock_queue.assert_not_called()


def test_reject_one_raises_409_when_already_rejected():
    campaign_id = uuid4()
    cc = _cc(campaign_id, pipeline_stage=PipelineStage.REJECTED)
    campaign_candidate_repo = MagicMock()
    campaign_candidate_repo.get_by_id.return_value = cc
    service = make_service(campaign_candidate_repo=campaign_candidate_repo)

    with pytest.raises(CampaignException) as exc_info:
        service.reject_one(campaign_id=campaign_id, campaign_candidate_id=cc.id, reason="did not meet the bar", actor_id="hm-1", actor_role="HIRING_MANAGER")

    assert exc_info.value.status_code == 409
