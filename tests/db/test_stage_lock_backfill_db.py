"""
Against Postgres (rolled back): scripts/backfill_stage_locks locks only the candidates the send task would have
locked, and leaves moved, already-locked and deliberately unlocked ones alone.
"""
from datetime import timedelta

from app.models.compliance import AuditLog
from app.models.email import EmailNotificationStatus as S, EmailTriggerEvent as E
from app.models.pipeline import PipelineStage
from scripts import backfill_stage_locks as backfill
from tests.db.test_send_policy_db import NOW, _outcome, _saved, db, world  # noqa: F401 - fixtures


def _mine(world):
    return [d for d in backfill.plan(world.db) if d.campaign_candidate_id == world.cc.id]


def _run(world):
    decisions = _mine(world)
    backfill.apply(world.db, decisions)
    world.db.refresh(world.cc)
    return decisions


def test_a_sent_rejection_email_is_locked_with_its_sent_time(world):
    sent = _saved(world, _outcome(world, E.CANDIDATE_REJECTED), status=S.SENT)
    decisions = _run(world)
    assert [d.outcome for d in decisions] == ["LOCK"]
    assert world.cc.stage_locked_at == NOW and world.cc.stage_lock_reason == "REJECTION_EMAIL_SENT"
    assert world.cc.stage_lock_notification_id == sent.id
    assert [d.outcome for d in _mine(world)] == ["ALREADY_LOCKED"]       # a second run writes nothing


def test_a_logged_selection_email_locks_a_selected_candidate(world):
    world.cc.pipeline_stage = PipelineStage.SELECTED
    world.db.flush()
    _saved(world, _outcome(world, E.CANDIDATE_SELECTED), status=S.LOGGED)
    _run(world)
    assert world.cc.stage_lock_reason == "SELECTION_EMAIL_SENT"


def test_emails_not_sent_lock_nothing(world):
    for status in (S.QUEUED, S.FAILED, S.DRAFT, S.DISCARDED, S.BLOCKED):
        _saved(world, _outcome(world, E.CANDIDATE_REJECTED, dedup_key=None), status=status)
    assert _mine(world) == []


def test_a_candidate_moved_since_the_email_is_left_alone(world):
    _saved(world, _outcome(world, E.CANDIDATE_REJECTED), status=S.SENT)
    world.cc.pipeline_stage = PipelineStage.SCREENING
    world.db.flush()
    assert [d.outcome for d in _run(world)] == ["STAGE_MOVED"]
    assert world.cc.stage_locked_at is None


def test_an_unlock_after_the_email_is_not_undone(world):
    _saved(world, _outcome(world, E.CANDIDATE_REJECTED), status=S.SENT)
    world.db.add(AuditLog(actor_id="zz-hr", actor_role="HR_ADMIN", action_type="STAGE_UNLOCKED",
                          entity_type="CAMPAIGN_CANDIDATE", entity_id=world.cc.id, campaign_id=world.campaign.id,
                          detail={"reason_code": "DATA_CORRECTION"}, created_at=NOW + timedelta(minutes=5)))
    world.db.flush()
    assert [d.outcome for d in _run(world)] == ["UNLOCKED_SINCE"]
    assert world.cc.stage_locked_at is None


def test_the_latest_outcome_email_decides(world):
    old = _outcome(world, E.CANDIDATE_SELECTED, dedup_key=None)
    _saved(world, old, status=S.SENT)
    old.sent_at = NOW - timedelta(days=3)
    newer = _saved(world, _outcome(world, E.CANDIDATE_REJECTED), status=S.SENT)
    world.db.flush()
    decisions = _run(world)
    assert [d.notification_id for d in decisions] == [newer.id]
    assert world.cc.stage_lock_reason == "REJECTION_EMAIL_SENT"


def test_the_dry_run_writes_nothing(world, capsys):
    _saved(world, _outcome(world, E.CANDIDATE_REJECTED), status=S.SENT)
    from unittest.mock import patch
    with patch("app.db.session.SessionLocal", return_value=world.db), patch.object(world.db, "close"), \
            patch.object(world.db, "rollback"):
        assert backfill.main([]) == 0
    world.db.refresh(world.cc)
    assert world.cc.stage_locked_at is None
    assert "dry run - nothing written" in capsys.readouterr().out

