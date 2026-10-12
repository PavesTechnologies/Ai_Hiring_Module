"""
"Score out of date": something a candidate's score depends on changed (their
resume was re-parsed elsewhere, or the campaign's JD calibration changed).
Scores are never changed automatically - candidates are stamped, the scorecard
shows a banner, and a recruiter re-scores under the existing rules (Screening:
full re-score; any later stage: score only, nobody moves).
"""
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.campaigns import CampaignStatus, HiringCampaign
from app.models.pipeline import CampaignCandidate

LIVE_CAMPAIGN_STATUSES = (CampaignStatus.ACTIVE, CampaignStatus.PAUSED)


def stamp_out_of_date(candidates, reason: str, stamp: datetime | None = None) -> int:
    """Stamp these campaign candidates (ORM objects) out of date with one reason."""
    stamp = stamp or datetime.now(timezone.utc)
    count = 0
    for candidate in candidates:
        candidate.score_out_of_date_at = stamp
        candidate.score_out_of_date_reason = reason
        count += 1
    return count


def live_campaigns_for_jd(db: Session, jd_id: UUID) -> list[HiringCampaign]:
    return (db.query(HiringCampaign)
            .filter(HiringCampaign.jd_id == jd_id, HiringCampaign.status.in_(LIVE_CAMPAIGN_STATUSES))
            .all())


def mark_campaigns_out_of_date(db: Session, campaign_ids: list[UUID], reason: str) -> int:
    """
    Stamp every already-scored candidate in these campaigns out of date (one
    UPDATE). Candidates not scored yet are left alone - their first score uses
    the new inputs anyway.
    """
    if not campaign_ids:
        return 0
    return (db.query(CampaignCandidate)
            .filter(CampaignCandidate.campaign_id.in_(campaign_ids),
                    CampaignCandidate.deterministic_score.isnot(None))
            .update({CampaignCandidate.score_out_of_date_at: datetime.now(timezone.utc),
                     CampaignCandidate.score_out_of_date_reason: reason},
                    synchronize_session=False))
