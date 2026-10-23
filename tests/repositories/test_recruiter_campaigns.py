"""
One definition of "a recruiter's campaigns" - assigned recruiter
(hiring_campaigns.recruiter_id) or creator (created_by); uploading to a
campaign does not make it theirs - and every scoping query uses it: the
interview calendar/scope check, the recruiter dashboard's campaign list and
nav badges, and the bulk-upload history. The SQL is compiled, never run; no
database, no email.
"""
import re
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql

from app.repositories.bulk_upload_job_repository import BulkUploadJobRepository
from app.repositories.dashboard_repository import DashboardRepository
from app.repositories.recruiter_campaigns import is_recruiters_campaign, recruiter_campaign_ids


def _sql(stmt) -> str:
    return str(stmt.compile(dialect=postgresql.dialect())).lower()


def _is_the_shared_definition(sql: str):
    where = re.split(r"\swhere\s", sql, maxsplit=1)[1]  # the scoping, not the columns a query selects
    assert "hiring_campaigns.recruiter_id = " in where and "hiring_campaigns.created_by = " in where
    assert re.search(r"\sor\s", where)
    assert "resumes" not in where and "uploaded_by" not in where                  # uploading isn't owning


@pytest.mark.parametrize("campaign, user_id, expected", [
    (SimpleNamespace(recruiter_id="rec-1", created_by="hr-1"), "rec-1", True),     # assigned recruiter
    (SimpleNamespace(recruiter_id="rec-1", created_by="rec-2"), "rec-2", True),    # creator
    (SimpleNamespace(recruiter_id="rec-1", created_by="hr-1"), "rec-3", False),    # e.g. only uploaded to it
    (None, "rec-1", False),
    (SimpleNamespace(recruiter_id="rec-1", created_by="hr-1"), None, False),
])
def test_is_recruiters_campaign(campaign, user_id, expected):
    assert is_recruiters_campaign(campaign, user_id) is expected


def test_the_query_form_is_assigned_or_creator():
    _is_the_shared_definition(_sql(recruiter_campaign_ids("rec-1")))


def test_interview_scope_check_uses_it():
    db = MagicMock()
    db.execute.return_value.first.return_value = (uuid4(),)
    assert DashboardRepository(db).is_campaign_accessible_to_recruiter("rec-1", uuid4()) is True
    _is_the_shared_definition(_sql(db.execute.call_args.args[0]))


def test_dashboard_scoping_uses_it():
    _is_the_shared_definition(_sql(DashboardRepository(MagicMock())._recruiter_campaign_ids("rec-1").select()))


def test_the_uploaded_to_metric_still_counts_uploads():
    sql = _sql(DashboardRepository(MagicMock())._campaigns_uploaded_to_ids("rec-1").select())
    assert "resumes.uploaded_by" in sql and "bulk_upload_jobs.uploaded_by" in sql


@pytest.mark.parametrize("method, args", [("list_by_recruiter", ("rec-1", 0, 10)), ("count_by_recruiter", ("rec-1",))])
def test_bulk_upload_history_uses_it(method, args):
    db = MagicMock()
    getattr(BulkUploadJobRepository(db), method)(*args)
    _is_the_shared_definition(_sql(db.execute.call_args.args[0]))
