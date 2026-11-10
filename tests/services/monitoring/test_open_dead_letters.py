"""
The monitoring panel's "Open dead letters" counts only entries that are neither replayed nor resolved,
and, with a campaign chosen, only that campaign's - it counted resolved entries and ignored the campaign.
"""
from datetime import datetime, timezone
from unittest.mock import MagicMock
from uuid import uuid4

from app.services.monitoring.monitoring_service import task_health


def _dead_letter_sql(campaign_id):
    db = MagicMock()
    db.execute.return_value.all.return_value = []
    task_health(db, since=datetime.now(timezone.utc), campaign_id=campaign_id)
    for call in db.execute.call_args_list:
        sql = " ".join(str(call.args[0]).split()).lower()
        if "from dead_letter_queue" in sql:
            return sql, (call.args[1] if len(call.args) > 1 else {})
    raise AssertionError("no dead-letter query")


def test_resolved_entries_are_not_open():
    sql, _ = _dead_letter_sql(None)
    assert "replayed_at is null" in sql and "resolved_at is null" in sql
    assert "campaign_id" not in sql.split("where", 1)[1]          # org-wide without a campaign


def test_a_chosen_campaign_scopes_the_count():
    campaign_id = uuid4()
    sql, params = _dead_letter_sql(campaign_id)
    assert "cc.id = d.campaign_candidate_id" in sql and "cc.resume_id = d.resume_id" in sql
    assert params["campaign_id"] == campaign_id
