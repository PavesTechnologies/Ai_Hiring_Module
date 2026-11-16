"""
Against Postgres (rolled back): the stage lock (email plan, phase 4). Rows are cloned from dev's synthetic data
inside the test's transaction; nothing is dispatched and no email is sent (the send task runs with
EMAIL_DELIVERY=log, the sender only logs).
"""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from app.exceptions.campaign_exceptions import CampaignException
from app.models.compliance import AuditLog
from app.models.email import EmailNotification, EmailNotificationStatus as S, EmailTriggerEvent as E
from app.models.pipeline import PipelineStage, TransitionSource
from app.services.campaign import stage_lock
from app.services.notifications import send_policy
from tests.db.test_send_policy_db import NOW, _outcome, _saved, db, world  # noqa: F401 - fixtures

HR = SimpleNamespace(user_id="zz-hr", roles=["HR_ADMIN"], authorised_roles=())


def _audit(db):  # noqa: F811
    from app.repositories.audit_repository import AuditRepository
    from app.services.audit_service import AuditService
    return AuditService(AuditRepository(db))


def _send(world, notification):
    """The real send task, on the test's session, with EMAIL_DELIVERY=log."""
    from app.tasks.email_tasks import send_candidate_email_task
    with patch("app.tasks.email_tasks.SessionLocal", return_value=world.db), patch.object(world.db, "close"):
        send_candidate_email_task.apply(kwargs={"email_notification_id": str(notification.id)})
    world.db.refresh(notification)
    world.db.refresh(world.cc)


def _locked(world):
    queued = _saved(world, _outcome(world, E.CANDIDATE_REJECTED), status=S.QUEUED)
    _send(world, queued)
    return queued


def test_sending_the_rejection_email_locks_the_stage(world):
    sent = _locked(world)
    assert sent.status == S.LOGGED                                       # EMAIL_DELIVERY=log in tests
    assert world.cc.stage_locked_at is not None and world.cc.stage_lock_reason == "REJECTION_EMAIL_SENT"
    assert world.cc.stage_lock_notification_id == sent.id
    assert stage_lock.lock_state(world.db, world.cc).locked


def test_an_email_being_sent_already_blocks_moves(world):
    _saved(world, _outcome(world, E.CANDIDATE_REJECTED), status=S.QUEUED)
    with pytest.raises(CampaignException) as refused:
        stage_lock.refuse_if_locked(world.db, world.cc)
    assert refused.value.status_code == 409 and refused.value.data["lock_reason"] == "REJECTION_EMAIL_PENDING"


def test_a_failed_send_sets_no_lock(world):
    _saved(world, _outcome(world, E.CANDIDATE_REJECTED), status=S.FAILED)
    assert not stage_lock.lock_state(world.db, world.cc).locked


def test_a_persons_move_is_refused_with_409(world):
    from app.repositories.CampaignRepository import CampaignRepository
    _locked(world)
    with pytest.raises(CampaignException) as refused:
        CampaignRepository(world.db).transition_candidate_stage(
            world.cc, PipelineStage.SCREENING, changed_by="zz-hr", change_reason="test",
            transition_source=TransitionSource.MANUAL)
    assert refused.value.status_code == 409 and refused.value.data["code"] == "STAGE_LOCKED"


def test_the_pipeline_transition_service_refuses_too(world):
    from app.repositories.allowed_transition_repository import AllowedTransitionRepository
    from app.repositories.campaign_candidate_repository import CampaignCandidateRepository
    from app.services.campaign.pipeline_transition_service import PipelineTransitionService
    _locked(world)
    service = PipelineTransitionService(allowed_transition_repo=AllowedTransitionRepository(world.db),
                                        campaign_candidate_repo=CampaignCandidateRepository(world.db),
                                        audit_service=MagicMock(), interview_schedule_repo=MagicMock(),
                                        campaign_repo=MagicMock())
    with pytest.raises(CampaignException) as refused:
        service.transition_stage(world.cc, PipelineStage.SCREENING, changed_by="zz-hr", actor_role="RECRUITER",
                                 reason="test", source=TransitionSource.MANUAL)
    assert refused.value.status_code == 409


def test_the_database_refuses_a_stage_change_on_a_locked_row(world):
    _locked(world)
    savepoint = world.db.begin_nested()
    with pytest.raises(DBAPIError) as refused:
        world.db.execute(text("UPDATE campaign_candidates SET pipeline_stage = 'SCREENING' WHERE id = :id"),
                         {"id": world.cc.id})
    savepoint.rollback()
    assert "STAGE_LOCKED" in str(refused.value)


def test_an_automated_move_is_refused(world):
    from app.repositories.allowed_transition_repository import AllowedTransitionRepository
    from app.repositories.campaign_candidate_repository import CampaignCandidateRepository
    from app.services.campaign.stage_transition_service import StageTransitionService
    _locked(world)
    service = StageTransitionService(allowed_transition_repo=AllowedTransitionRepository(world.db),
                                     campaign_candidate_repo=CampaignCandidateRepository(world.db),
                                     audit_service=MagicMock(), interview_schedule_repo=MagicMock())
    assert service._system_move_permitted(world.cc, PipelineStage.SCREENING) is False


def test_unlock_needs_a_reason_code_and_is_audited(world):
    sent = _locked(world)
    with pytest.raises(CampaignException) as no_reason:
        stage_lock.unlock(world.db, world.cc.id, reason_code="NOT_A_CODE", actor=HR, audit_service=_audit(world.db))
    assert no_reason.value.status_code == 422
    stage_lock.unlock(world.db, world.cc.id, reason_code="EMAIL_SENT_IN_ERROR", actor=HR, audit_service=_audit(world.db))
    world.db.refresh(world.cc)
    assert world.cc.stage_locked_at is None and not stage_lock.lock_state(world.db, world.cc).locked
    entry = world.db.execute(select(AuditLog).where(AuditLog.entity_id == world.cc.id,
                                                    AuditLog.action_type == "STAGE_UNLOCKED")).scalars().one()
    assert entry.detail["reason_code"] == "EMAIL_SENT_IN_ERROR" and entry.detail["notification_id"] == str(sent.id)
    assert entry.actor_role == "HR_ADMIN"
    # the stage can move again (the trigger allows it), and the email still can't be sent twice
    world.db.execute(text("UPDATE campaign_candidates SET pipeline_stage = 'SCREENING' WHERE id = :id"),
                     {"id": world.cc.id})
    world.db.execute(text("UPDATE campaign_candidates SET pipeline_stage = 'REJECTED' WHERE id = :id"),
                     {"id": world.cc.id})
    world.db.refresh(world.cc)
    again = _outcome(world, E.CANDIDATE_REJECTED, dedup_key=None)
    assert "ALREADY_SENT" in {p.code for p in send_policy.problems(world.db, again)}


def test_unlocking_an_unlocked_candidate_is_refused(world):
    with pytest.raises(CampaignException) as refused:
        stage_lock.unlock(world.db, world.cc.id, reason_code="DATA_CORRECTION", actor=HR, audit_service=_audit(world.db))
    assert refused.value.status_code == 409


def test_unlock_reasons_are_the_stage_unlock_kind_only(world):
    from app.services.campaign.outcome_overrides import reason_codes
    unlock = stage_lock.unlock_reason_codes(world.db)
    assert {"EMAIL_SENT_IN_ERROR", "CANDIDATE_REENGAGED", "DATA_CORRECTION"} <= set(unlock)
    assert not set(unlock) & set(reason_codes(world.db))          # never offered as an override reason
