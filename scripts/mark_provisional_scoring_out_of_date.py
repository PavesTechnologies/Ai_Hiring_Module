"""
One-off, at release of provisional scoring (Phase 2 Step 2): mark already-
scored candidates in live campaigns 'score out of date', because their scores
were computed before unresolved JD unknown skills counted in the skill score
(tier weight x 0.8 when found, 0 when not).

Default scope: live (ACTIVE / PAUSED) campaigns whose JD has at least one
unresolved unknown skill - the only scores this release changes. --all marks
every live campaign. Nothing is re-scored and nothing is sent.

Dry run by default:
    python -m scripts.mark_provisional_scoring_out_of_date           # show what would change
    python -m scripts.mark_provisional_scoring_out_of_date --apply   # write it
"""
import argparse

from sqlalchemy import func

from app.db.session import SessionLocal
import app.models  # noqa: F401 - registers every mapper
from app.models.campaigns import HiringCampaign
from app.models.pipeline import CampaignCandidate
from app.models.skills import JDUnknownSkill, JDUnknownSkillStatus
from app.services.campaign.score_out_of_date import LIVE_CAMPAIGN_STATUSES, mark_campaigns_out_of_date

REASON = ("Scoring changed: skills on the JD that aren't in the catalogue yet now count in the skill score "
          "(0.8 credit when found in the resume, 0 when not). These scores were computed before that.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="write the changes (default: dry run)")
    parser.add_argument("--all", action="store_true", help="every live campaign, not only those with unknown skills")
    args = parser.parse_args()

    db = SessionLocal()
    try:
        pending = dict(
            db.query(JDUnknownSkill.jd_id, func.count())
            .filter(JDUnknownSkill.status == JDUnknownSkillStatus.PENDING)
            .group_by(JDUnknownSkill.jd_id).all()
        )
        campaigns = db.query(HiringCampaign).filter(HiringCampaign.status.in_(LIVE_CAMPAIGN_STATUSES)).all()
        chosen = [c for c in campaigns if args.all or pending.get(c.jd_id)]
        for campaign in campaigns:
            scored = (db.query(func.count(CampaignCandidate.id))
                      .filter(CampaignCandidate.campaign_id == campaign.id,
                              CampaignCandidate.deterministic_score.isnot(None)).scalar())
            marker = "mark" if campaign in chosen else "skip"
            print(f"{marker}  {campaign.name[:40]!r:42} {campaign.status.value:<7} "
                  f"unknown skills on JD: {pending.get(campaign.jd_id, 0):<3} scored candidates: {scored}")
        if not args.apply:
            print(f"Dry run - {len(chosen)} of {len(campaigns)} live campaign(s) would be marked. "
                  f"Re-run with --apply to write it.")
            db.rollback()
            return
        marked = mark_campaigns_out_of_date(db, [c.id for c in chosen], REASON) if chosen else 0
        db.commit()
        print(f"Marked {marked} candidate(s) in {len(chosen)} campaign(s) 'score out of date'.")
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    main()
