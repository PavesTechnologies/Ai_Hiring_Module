"""The open-dead-letter count runs on real Postgres (rolled back), with and without a campaign."""
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from app.services.monitoring.monitoring_service import task_health
from tests.db.test_campaign_candidate_delete_scored import db  # noqa: F401 - fixture


def test_the_count_runs_with_and_without_a_campaign(db):  # noqa: F811
    since = datetime.now(timezone.utc) - timedelta(days=30)
    assert isinstance(task_health(db, since=since, campaign_id=None)["open_dead_letters"], list)
    assert task_health(db, since=since, campaign_id=uuid4())["open_dead_letters"] == []
