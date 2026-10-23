"""
A recruiter's campaigns - the one definition the whole codebase uses: the
campaigns they are the assigned recruiter of (hiring_campaigns.recruiter_id)
or created (hiring_campaigns.created_by). Uploading resumes to a campaign does
not make it theirs.

Used for adding and removing candidates, the interview calendar and
interview scope, the recruiter dashboard's campaign list and nav badges, and
the cross-campaign bulk-upload history.
"""
from sqlalchemy import or_, select

from app.models.campaigns import HiringCampaign


def recruiter_campaigns_filter(user_id: str):
    """WHERE clause on hiring_campaigns: this user's campaigns as a recruiter."""
    return or_(HiringCampaign.recruiter_id == user_id, HiringCampaign.created_by == user_id)


def recruiter_campaign_ids(user_id: str):
    """SELECT of the ids of this user's campaigns as a recruiter."""
    return select(HiringCampaign.id).where(recruiter_campaigns_filter(user_id))


def is_recruiters_campaign(campaign, user_id: str | None) -> bool:
    """The same rule for a campaign row already loaded."""
    if campaign is None or not user_id:
        return False
    return user_id in (campaign.recruiter_id, campaign.created_by)
