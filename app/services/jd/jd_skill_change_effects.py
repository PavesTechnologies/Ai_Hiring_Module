"""
What follows whenever a JD's skills change through unknown-skill resolution
(Phase 2 Step 2) - the per-JD map / approve as new / drop, and every global
curation path (map, promote, create, approve, dismiss, delete). Each JD
touched gets the same handling, inside the caller's transaction:

  1. is_verified recomputed (VERIFIED once nothing is left unresolved),
  2. a calibrated JD saves the change as its next calibration version,
  3. already-scored candidates in its live campaigns are marked out of date
     (their scores counted the skill provisionally, or not at all).

Never commits; never sends anything.
"""
from uuid import UUID

from sqlalchemy.orm import Session

from app.repositories.jd_calibration_repository import JDCalibrationRepository
from app.repositories.skill_repository import SkillRepository
from app.services.campaign.score_out_of_date import live_campaigns_for_jd, mark_campaigns_out_of_date



def apply_jd_skill_change_effects(
    db: Session, jd_ids, *, actor_id: str | None, actor_role: str, note: str, audit_service,
    skill_repository: SkillRepository | None = None,
) -> dict[UUID, dict]:
    """{jd_id: {"calibration_version": int | None, "candidates_marked_out_of_date": int}}."""
    from app.services.jd.jd_calibration_service import JDCalibrationService

    skill_repository = skill_repository or SkillRepository(db)
    calibration = JDCalibrationService(JDCalibrationRepository(db), audit_service)
    results: dict[UUID, dict] = {}
    for jd_id in dict.fromkeys(jd_ids):
        skill_repository.mark_jd_verified_if_fully_resolved(jd_id)
        version = calibration.record_skill_change(jd_id, actor_id=actor_id, actor_role=actor_role, note=note) \
            if actor_id else None
        jd = calibration.repo.get_jd(jd_id)
        title = getattr(jd, "title", None) or "this JD"
        live_ids = [c.id for c in live_campaigns_for_jd(db, jd_id)]
        marked = mark_campaigns_out_of_date(db, live_ids, (
            f"The JD '{title}' skills changed: {note}. These scores were computed before the change."
        )) if live_ids else 0
        results[jd_id] = {"calibration_version": version, "candidates_marked_out_of_date": marked}
    return results
