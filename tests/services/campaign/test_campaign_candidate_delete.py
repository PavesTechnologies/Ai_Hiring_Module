from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from app.exceptions.campaign_exceptions import CampaignException
from app.services.campaign.campaign_candidate_service import CampaignCandidateService

"""
DELETE .../campaign-candidates/{campaign_candidate_id} - delete_campaign_candidate.

Scoped strictly to ONE campaign's link row: removing a candidate from
Campaign A must never touch their row in Campaign B (or the shared
Candidate/Resume rows). This is the counterpart to the separate, deliberate
GDPR-style DELETE /candidates/{candidate_id} (CandidateErasureService),
which *does* purge a candidate everywhere - the two must never be confused.
"""


def make_service(**overrides):
    defaults = dict(campaign_repo=MagicMock(), campaign_candidate_repo=MagicMock(), audit_service=MagicMock())
    defaults.update(overrides)
    return CampaignCandidateService(**defaults)


def make_service_with_dependent_repo_mocks(**overrides):
    """
    Same as make_service, but with explicit MagicMocks for every repo
    delete_campaign_candidate now cleans up before the parent row - lets
    tests assert on them directly instead of relying on the constructor's
    lazy campaign_candidate_repo.db-derived defaults.
    """
    defaults = dict(
        email_notification_repo=MagicMock(),
        candidate_note_repo=MagicMock(),
        interview_schedule_repo=MagicMock(),
        composite_score_history_repo=MagicMock(),
        dead_letter_queue_repo=MagicMock(),
        celery_task_log_repo=MagicMock(),
    )
    defaults.update(overrides)
    return make_service(**defaults)


def _campaign_candidate(**overrides):
    defaults = dict(id=uuid4(), campaign_id=uuid4(), candidate_id=uuid4(), resume_id=uuid4())
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def test_delete_campaign_candidate_raises_404_when_not_found():
    campaign_candidate_repo = MagicMock()
    campaign_candidate_repo.get_by_id.return_value = None
    service = make_service(campaign_candidate_repo=campaign_candidate_repo)

    with pytest.raises(CampaignException) as exc_info:
        service.delete_campaign_candidate(uuid4(), actor_id="user-1")

    assert exc_info.value.status_code == 404


def test_delete_campaign_candidate_deletes_only_the_targeted_row():
    """The repo call must receive exactly the looked-up row for this campaign_candidate_id - never a bulk/by-candidate_id delete."""
    cc = _campaign_candidate()
    campaign_candidate_repo = MagicMock()
    campaign_candidate_repo.get_by_id.return_value = cc
    service = make_service(campaign_candidate_repo=campaign_candidate_repo)

    with patch("app.services.campaign.campaign_candidate_service.publish_board_candidate_removed"):
        service.delete_campaign_candidate(cc.id, actor_id="user-1")

    campaign_candidate_repo.delete.assert_called_once_with(cc)
    campaign_candidate_repo.commit.assert_called_once()


def test_delete_campaign_candidate_never_touches_candidate_or_resume_repos():
    """
    Structural guarantee: this method has no candidate_repo/resume_repo
    delete call at all - deleting a candidate from one campaign cannot
    reach the shared Candidate/Resume rows (and therefore cannot affect
    any other campaign that candidate is also in).
    """
    cc = _campaign_candidate()
    campaign_candidate_repo = MagicMock()
    campaign_candidate_repo.get_by_id.return_value = cc
    candidate_repo = MagicMock()
    resume_repo = MagicMock()
    service = make_service(
        campaign_candidate_repo=campaign_candidate_repo,
        candidate_repo=candidate_repo,
        resume_repo=resume_repo,
    )

    with patch("app.services.campaign.campaign_candidate_service.publish_board_candidate_removed"):
        service.delete_campaign_candidate(cc.id, actor_id="user-1")

    candidate_repo.delete.assert_not_called()
    resume_repo.delete.assert_not_called()


def test_delete_campaign_candidate_only_looks_up_by_its_own_id_not_by_candidate_id():
    """
    Confirms the lookup key is the campaign_candidate row's own id (unique
    per campaign+candidate+resume), not the shared candidate_id - so a
    candidate present in two campaigns has two distinct, independently
    deletable rows.
    """
    cc = _campaign_candidate()
    campaign_candidate_repo = MagicMock()
    campaign_candidate_repo.get_by_id.return_value = cc
    service = make_service(campaign_candidate_repo=campaign_candidate_repo)

    with patch("app.services.campaign.campaign_candidate_service.publish_board_candidate_removed"):
        service.delete_campaign_candidate(cc.id, actor_id="user-1")

    campaign_candidate_repo.get_by_id.assert_called_once_with(cc.id)


def test_delete_campaign_candidate_rolls_back_on_failure():
    cc = _campaign_candidate()
    campaign_candidate_repo = MagicMock()
    campaign_candidate_repo.get_by_id.return_value = cc
    campaign_candidate_repo.delete.side_effect = RuntimeError("db error")
    service = make_service(campaign_candidate_repo=campaign_candidate_repo)

    with pytest.raises(RuntimeError):
        service.delete_campaign_candidate(cc.id, actor_id="user-1")

    campaign_candidate_repo.rollback.assert_called_once()
    campaign_candidate_repo.commit.assert_not_called()


def test_delete_campaign_candidate_logs_audit_entry_scoped_to_its_own_campaign():
    cc = _campaign_candidate()
    campaign_candidate_repo = MagicMock()
    campaign_candidate_repo.get_by_id.return_value = cc
    audit_service = MagicMock()
    service = make_service(campaign_candidate_repo=campaign_candidate_repo, audit_service=audit_service)

    with patch("app.services.campaign.campaign_candidate_service.publish_board_candidate_removed"):
        service.delete_campaign_candidate(cc.id, actor_id="user-1", actor_role="HR_ADMIN")

    _, kwargs = audit_service.log.call_args
    assert kwargs["campaign_id"] == cc.campaign_id
    assert kwargs["entity_id"] == cc.id
    assert kwargs["details"]["candidate_id"] == str(cc.candidate_id)


# ----------------------------------------------------------------------
# Bug fix: every one of these tables carries an FK to
# campaign_candidates.id with no ON DELETE rule (confirmed live -
# psycopg2.errors.ForeignKeyViolation on campaign_candidate_stage_history
# for a candidate that had merely been auto-scored past UPLOADED, before
# this fix). delete_campaign_candidate must clear all of them, scoped to
# this one campaign_candidate_id only, before the parent row can go -
# mirroring CandidateErasureService.erase_candidate's already-correct
# per-candidate version of the same cleanup.
# ----------------------------------------------------------------------

def test_delete_campaign_candidate_clears_every_dependent_table_before_the_parent_row():
    cc = _campaign_candidate()
    campaign_candidate_repo = MagicMock()
    campaign_candidate_repo.get_by_id.return_value = cc
    service = make_service_with_dependent_repo_mocks(campaign_candidate_repo=campaign_candidate_repo)

    with patch("app.services.campaign.campaign_candidate_service.publish_board_candidate_removed"):
        service.delete_campaign_candidate(cc.id, actor_id="user-1")

    service.email_notification_repo.delete_by_campaign_candidate_id.assert_called_once_with(cc.id)
    campaign_candidate_repo.delete_stage_history.assert_called_once_with(cc.id)
    campaign_candidate_repo.delete_stage_transition_log.assert_called_once_with(cc.id)
    service.candidate_note_repo.delete_by_campaign_candidate_id.assert_called_once_with(cc.id)
    service.interview_schedule_repo.delete_by_campaign_candidate_id.assert_called_once_with(cc.id)
    service.composite_score_history_repo.delete_by_campaign_candidate_id.assert_called_once_with(cc.id)
    service.dead_letter_queue_repo.delete_by_campaign_candidate_id.assert_called_once_with(cc.id)
    service.celery_task_log_repo.delete_by_campaign_candidate_id.assert_called_once_with(cc.id)
    campaign_candidate_repo.delete.assert_called_once_with(cc)


def test_delete_campaign_candidate_clears_dead_letter_queue_before_celery_task_log():
    """dead_letter_queue.original_task_id is a NOT NULL FK to celery_task_log.task_id - reversing this order would just move the ForeignKeyViolation, not fix it."""
    cc = _campaign_candidate()
    campaign_candidate_repo = MagicMock()
    campaign_candidate_repo.get_by_id.return_value = cc
    manager = MagicMock()
    dead_letter_queue_repo = MagicMock()
    celery_task_log_repo = MagicMock()
    manager.attach_mock(dead_letter_queue_repo.delete_by_campaign_candidate_id, "dlq_delete")
    manager.attach_mock(celery_task_log_repo.delete_by_campaign_candidate_id, "task_log_delete")
    service = make_service_with_dependent_repo_mocks(
        campaign_candidate_repo=campaign_candidate_repo,
        dead_letter_queue_repo=dead_letter_queue_repo,
        celery_task_log_repo=celery_task_log_repo,
    )

    with patch("app.services.campaign.campaign_candidate_service.publish_board_candidate_removed"):
        service.delete_campaign_candidate(cc.id, actor_id="user-1")

    call_order = [call[0] for call in manager.mock_calls]
    assert call_order.index("dlq_delete") < call_order.index("task_log_delete")


def test_delete_campaign_candidate_clears_dependents_before_deleting_the_parent_row():
    cc = _campaign_candidate()
    campaign_candidate_repo = MagicMock()
    campaign_candidate_repo.get_by_id.return_value = cc
    manager = MagicMock()
    candidate_note_repo = MagicMock()
    manager.attach_mock(candidate_note_repo.delete_by_campaign_candidate_id, "clear_notes")
    manager.attach_mock(campaign_candidate_repo.delete, "delete_parent")
    service = make_service_with_dependent_repo_mocks(
        campaign_candidate_repo=campaign_candidate_repo,
        candidate_note_repo=candidate_note_repo,
    )

    with patch("app.services.campaign.campaign_candidate_service.publish_board_candidate_removed"):
        service.delete_campaign_candidate(cc.id, actor_id="user-1")

    call_order = [call[0] for call in manager.mock_calls]
    assert call_order.index("clear_notes") < call_order.index("delete_parent")


def test_delete_campaign_candidate_dependent_cleanup_is_scoped_to_this_campaign_candidate_only():
    """The exact isolation guarantee from earlier in this test file, now re-checked against every new cleanup call."""
    cc = _campaign_candidate()
    other_id = uuid4()
    campaign_candidate_repo = MagicMock()
    campaign_candidate_repo.get_by_id.return_value = cc
    service = make_service_with_dependent_repo_mocks(campaign_candidate_repo=campaign_candidate_repo)

    with patch("app.services.campaign.campaign_candidate_service.publish_board_candidate_removed"):
        service.delete_campaign_candidate(cc.id, actor_id="user-1")

    for mock_call in (
        service.email_notification_repo.delete_by_campaign_candidate_id,
        service.candidate_note_repo.delete_by_campaign_candidate_id,
        service.interview_schedule_repo.delete_by_campaign_candidate_id,
        service.composite_score_history_repo.delete_by_campaign_candidate_id,
        service.dead_letter_queue_repo.delete_by_campaign_candidate_id,
        service.celery_task_log_repo.delete_by_campaign_candidate_id,
    ):
        called_with = mock_call.call_args.args[0]
        assert called_with == cc.id
        assert called_with != other_id


def test_delete_campaign_candidate_rolls_back_when_a_dependent_cleanup_call_fails():
    cc = _campaign_candidate()
    campaign_candidate_repo = MagicMock()
    campaign_candidate_repo.get_by_id.return_value = cc
    candidate_note_repo = MagicMock()
    candidate_note_repo.delete_by_campaign_candidate_id.side_effect = RuntimeError("db error")
    service = make_service_with_dependent_repo_mocks(
        campaign_candidate_repo=campaign_candidate_repo,
        candidate_note_repo=candidate_note_repo,
    )

    with pytest.raises(RuntimeError):
        service.delete_campaign_candidate(cc.id, actor_id="user-1")

    campaign_candidate_repo.rollback.assert_called_once()
    campaign_candidate_repo.delete.assert_not_called()
    campaign_candidate_repo.commit.assert_not_called()
