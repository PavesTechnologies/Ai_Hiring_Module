"""Against Postgres (rolled back): a reference-data edit's impact, and the out-of-date marking after it."""
from sqlalchemy import select

from app.models.campaigns import CampaignStatus
from app.models.skills import JDSkill, SkillOntology
from app.services.settings.reference_impact import impact, mark_after_edit
from tests.db.test_campaign_candidate_delete_scored import db, scored  # noqa: F401 - fixtures


def _live_campaign_using_a_skill(db, scored):  # noqa: F811
    campaign, cc = scored["campaign"], scored["cc"]
    campaign.status = CampaignStatus.ACTIVE
    skill_id = db.execute(select(JDSkill.canonical_skill_id).where(JDSkill.jd_id == campaign.jd_id).limit(1)).scalar()
    if skill_id is None:
        skill_id = db.execute(select(SkillOntology.id).limit(1)).scalar()
        db.add(JDSkill(jd_id=campaign.jd_id, canonical_skill_id=skill_id))
    cc.deterministic_score = 55
    cc.score_out_of_date_at = None
    db.flush()
    return campaign, cc, skill_id


def test_a_skill_edit_names_the_live_campaigns_using_it_and_their_scored_candidates(db, scored):  # noqa: F811
    campaign, _, skill_id = _live_campaign_using_a_skill(db, scored)
    result = impact(db, "skill", skill_ids=[skill_id])
    assert str(campaign.id) in {c["id"] for c in result["campaigns"]}
    assert result["candidates_marked_out_of_date"] >= 1


def test_the_edit_stamps_those_candidates_out_of_date(db, scored):  # noqa: F811
    campaign, cc, skill_id = _live_campaign_using_a_skill(db, scored)
    assert mark_after_edit(db, "skill", "The catalogue skill 'x'", skill_ids=[skill_id]) >= 1
    db.refresh(cc)
    assert cc.score_out_of_date_at is not None and "The catalogue skill 'x' changed" in cc.score_out_of_date_reason


def test_a_closed_campaign_is_not_affected(db, scored):  # noqa: F811
    campaign, cc, skill_id = _live_campaign_using_a_skill(db, scored)
    campaign.status = CampaignStatus.CLOSED
    db.flush()
    assert str(campaign.id) not in {c["id"] for c in impact(db, "skill", skill_ids=[skill_id])["campaigns"]}


def test_a_skill_no_live_jd_uses_affects_nothing(db):  # noqa: F811
    assert impact(db, "skill", skill_ids=["00000000-0000-0000-0000-000000000001"]) == {
        "campaigns": [], "candidates_marked_out_of_date": 0}
