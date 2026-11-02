"""The scoring recovery job's query against real Postgres (rolled back): candidates whose decision never landed."""
from datetime import datetime, timedelta, timezone

from app.models.campaigns import CampaignStatus
from app.models.pipeline import PipelineStage
from app.repositories.campaign_candidate_repository import CampaignCandidateRepository
from tests.db.test_campaign_candidate_delete_scored import db, scored  # noqa: F401 - fixtures

SCOREABLE = {CampaignStatus.ACTIVE, CampaignStatus.PAUSED}


def _ids(db, campaign_statuses=SCOREABLE, created_before=None):  # noqa: F811
    cutoff = created_before or datetime.now(timezone.utc) + timedelta(minutes=1)
    return {cc.id for cc in CampaignCandidateRepository(db).get_never_screened(campaign_statuses, created_before=cutoff)}


def test_a_never_screened_candidate_is_found_and_a_screened_one_is_not(db, scored):  # noqa: F811
    cc, campaign = scored["cc"], scored["campaign"]
    campaign.status = CampaignStatus.ACTIVE
    for stage in (PipelineStage.UPLOADED, PipelineStage.SCREENING):
        cc.pipeline_stage, cc.screened_at = stage, None
        db.flush()
        assert cc.id in _ids(db)

    cc.screened_at = datetime.now(timezone.utc)                 # the decision landed
    db.flush()
    assert cc.id not in _ids(db)

    cc.screened_at, cc.pipeline_stage = None, PipelineStage.HOLD  # moved on, whatever the timestamp says
    db.flush()
    assert cc.id not in _ids(db)


def test_closed_campaigns_and_just_added_candidates_are_left_alone(db, scored):  # noqa: F811
    cc, campaign = scored["cc"], scored["campaign"]
    cc.pipeline_stage, cc.screened_at = PipelineStage.SCREENING, None
    campaign.status = CampaignStatus.CLOSED
    db.flush()
    assert cc.id not in _ids(db)

    campaign.status = CampaignStatus.ACTIVE
    db.flush()
    assert cc.id not in _ids(db, created_before=cc.created_at - timedelta(seconds=1))
