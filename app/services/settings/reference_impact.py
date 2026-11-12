"""
Reference data edits: their impact before saving, and the out-of-date marking after (settings phase 3,
agreed 2026-11-12). Reference data is what scoring reads besides the scoring version: the skill catalogue
(names, aliases, status), certification mappings, education field relations and profile templates.

An edit can change the next score of candidates already scored in live (active / paused) campaigns whose JD
uses what changed. The preview names those campaigns and counts their scored candidates; the save stamps those
candidates "score out of date" (never re-scored automatically - a recruiter re-scores, as for a JD change).
The current scoring version can then be checked on the golden set on demand (a reference-check golden run).
"""
from __future__ import annotations

from uuid import UUID

from sqlalchemy import func, select

from app.models.campaigns import HiringCampaign
from app.models.jd.job_descriptions import JobDescription
from app.models.pipeline import CampaignCandidate
from app.services.campaign.score_out_of_date import LIVE_CAMPAIGN_STATUSES, mark_campaigns_out_of_date

KINDS = ("skill", "certification", "education_relation", "profile_template")


def _live(db):
    return (select(HiringCampaign.id, HiringCampaign.name)
            .where(HiringCampaign.status.in_(LIVE_CAMPAIGN_STATUSES)))


def campaigns_using_skills(db, skill_ids) -> list[tuple]:
    from app.models.skills import JDSkill
    ids = {UUID(str(s)) for s in skill_ids if s}
    if not ids:
        return []
    query = (_live(db).join(JDSkill, JDSkill.jd_id == HiringCampaign.jd_id)
             .where(JDSkill.canonical_skill_id.in_(ids)).distinct())
    return list(db.execute(query).all())


def campaigns_using_certification(db, certification_id) -> list[tuple]:
    from app.models.scoring_v2 import CertificationSkill
    skill_ids = db.execute(select(CertificationSkill.canonical_skill_id)
                           .where(CertificationSkill.certification_id == UUID(str(certification_id)))).scalars().all()
    return campaigns_using_skills(db, skill_ids)


def campaigns_using_education_domain(db, role_domain: str) -> list[tuple]:
    from app.services.jd.jd_calibration_rules import EDUCATION_MANDATORY, jd_education_requirement, jd_role_domain
    rows = db.execute(_live(db).add_columns(JobDescription)
                      .join(JobDescription, JobDescription.id == HiringCampaign.jd_id)).all()
    return [(cid, name) for cid, name, jd in rows
            if jd_role_domain(jd) == role_domain and jd_education_requirement(jd) == EDUCATION_MANDATORY]


def campaigns_using_template(db, key: str) -> list[tuple]:
    query = (_live(db).join(JobDescription, JobDescription.id == HiringCampaign.jd_id)
             .where(JobDescription.profile_template == key))
    return list(db.execute(query).all())


def affected_campaigns(db, kind: str, *, skill_ids=None, certification_id=None, role_domain=None,
                       template_key=None) -> list[tuple]:
    if kind == "skill":
        return campaigns_using_skills(db, skill_ids or [])
    if kind == "certification":
        return campaigns_using_certification(db, certification_id) if certification_id else \
            campaigns_using_skills(db, skill_ids or [])
    if kind == "education_relation":
        return campaigns_using_education_domain(db, role_domain)
    if kind == "profile_template":
        return campaigns_using_template(db, template_key)
    raise ValueError(f"unknown reference data kind: {kind}")


def impact(db, kind: str, **target) -> dict:
    campaigns = affected_campaigns(db, kind, **target)
    ids = [cid for cid, _ in campaigns]
    scored = db.execute(select(func.count()).select_from(CampaignCandidate).where(
        CampaignCandidate.campaign_id.in_(ids), CampaignCandidate.deterministic_score.isnot(None))).scalar() \
        if ids else 0
    return {"campaigns": [{"id": str(cid), "name": name} for cid, name in sorted(campaigns, key=lambda c: c[1] or "")],
            "candidates_marked_out_of_date": int(scored or 0)}


def mark_after_edit(db, kind: str, what: str, **target) -> int:
    """Stamp the scored candidates of the affected live campaigns; returns how many. Call before the commit."""
    ids = [cid for cid, _ in affected_campaigns(db, kind, **target)]
    if not ids:
        return 0
    from datetime import datetime, timezone
    return mark_campaigns_out_of_date(db, ids, (
        f"{what} changed on {datetime.now(timezone.utc):%Y-%m-%d}: these scores were computed before the change."))
