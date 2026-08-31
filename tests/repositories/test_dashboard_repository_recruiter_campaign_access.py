from unittest.mock import MagicMock
from uuid import uuid4

from app.repositories.dashboard_repository import DashboardRepository

"""
Campaign-wide interview calendar follow-up - is_campaign_accessible_to_recruiter,
the first caller of _recruiter_campaign_ids outside DashboardRepository
itself. Reuses that method's exact "campaigns I uploaded to, bulk-uploaded
to, or created" definition rather than HiringCampaign.recruiter_id (which
exists on the model but is never checked against the acting user anywhere
in this codebase).
"""


def test_returns_true_when_the_campaign_is_in_the_recruiters_accessible_set():
    db = MagicMock()
    db.execute.return_value.first.return_value = (uuid4(),)
    repo = DashboardRepository(db)

    result = repo.is_campaign_accessible_to_recruiter("recruiter-1", uuid4())

    assert result is True


def test_returns_false_when_the_campaign_is_not_in_the_recruiters_accessible_set():
    db = MagicMock()
    db.execute.return_value.first.return_value = None
    repo = DashboardRepository(db)

    result = repo.is_campaign_accessible_to_recruiter("recruiter-1", uuid4())

    assert result is False
