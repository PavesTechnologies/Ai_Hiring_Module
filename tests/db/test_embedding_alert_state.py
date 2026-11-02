"""The embedding-alert cooldown's state lookup against real Postgres (rolled back)."""
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from app.enums.constants import ActionType, EntityType
from app.models.compliance import AuditLog
from app.repositories.audit_repository import AuditRepository
from app.tasks.embedding_health_tasks import _ALERT_STATE_ACTIONS
from tests.db.test_campaign_candidate_delete_scored import db  # noqa: F401 - fixture

CONDITION = "EMBEDDING_FAILURE_RATE_EXCEEDED"


def _entry(db, campaign_id, action, minutes_ago, condition=CONDITION):  # noqa: F811
    db.add(AuditLog(actor_role="SYSTEM", action_type=action, entity_type=EntityType.CAMPAIGN, entity_id=campaign_id,
                    campaign_id=campaign_id, detail={"condition": condition},
                    created_at=datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)))


def test_the_latest_alert_state_and_the_last_send_per_campaign(db):  # noqa: F811
    campaign, other = uuid4(), uuid4()
    _entry(db, campaign, ActionType.PLATFORM_ALERT_SENT, 300)
    _entry(db, campaign, ActionType.PLATFORM_ALERT_SKIPPED, 60)
    _entry(db, campaign, ActionType.PLATFORM_ALERT_CLEARED, 30)
    _entry(db, campaign, ActionType.PLATFORM_ALERT_SENT, 5, condition="QUEUE_DEPTH_EXCEEDED")   # another alert
    _entry(db, other, ActionType.PLATFORM_ALERT_SKIPPED, 1)                                      # another campaign
    db.flush()
    repo = AuditRepository(db)

    latest = repo.get_latest_of(campaign, _ALERT_STATE_ACTIONS, CONDITION)
    assert latest.action_type == ActionType.PLATFORM_ALERT_CLEARED
    sent = repo.get_latest_of(campaign, (ActionType.PLATFORM_ALERT_SENT,), CONDITION)
    assert sent.action_type == ActionType.PLATFORM_ALERT_SENT and sent.detail["condition"] == CONDITION
    assert repo.get_latest_of(uuid4(), _ALERT_STATE_ACTIONS, CONDITION) is None
