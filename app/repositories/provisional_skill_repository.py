"""Unresolved JD unknown skills and the per-resume provisional scans (Phase 2 provisional scoring)."""
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.scoring_v2 import ResumeProvisionalSkillScan
from app.models.skills import JDUnknownSkill, JDUnknownSkillStatus, UnknownSkill
from app.services.skills.provisional_skill_match import ProvisionalTerm


class ProvisionalSkillRepository:
    def __init__(self, db: Session):
        self.db = db

    def pending_for_jd(self, jd_id: UUID) -> list[tuple[JDUnknownSkill, UnknownSkill]]:
        return (
            self.db.query(JDUnknownSkill, UnknownSkill)
            .join(UnknownSkill, UnknownSkill.id == JDUnknownSkill.unknown_skill_id)
            .filter(JDUnknownSkill.jd_id == jd_id, JDUnknownSkill.status == JDUnknownSkillStatus.PENDING)
            .order_by(UnknownSkill.raw_text)
            .all()
        )

    def all_pending_terms(self) -> list[ProvisionalTerm]:
        """Every unknown skill some JD still has unresolved - what detection scans resumes for."""
        rows = (
            self.db.query(UnknownSkill.id, UnknownSkill.raw_text)
            .join(JDUnknownSkill, JDUnknownSkill.unknown_skill_id == UnknownSkill.id)
            .filter(JDUnknownSkill.status == JDUnknownSkillStatus.PENDING)
            .distinct()
            .all()
        )
        return [ProvisionalTerm(unknown_skill_id=i, raw_text=t) for i, t in rows]

    def get_scan(self, resume_id: UUID) -> ResumeProvisionalSkillScan | None:
        return self.db.query(ResumeProvisionalSkillScan).filter(ResumeProvisionalSkillScan.resume_id == resume_id).first()

    def replace_scan(self, resume_id: UUID, *, text_hash: str, detection_version: str,
                     scanned_ids: list[UUID], hits: list[dict]) -> ResumeProvisionalSkillScan:
        row = self.get_scan(resume_id)
        if row is None:
            row = ResumeProvisionalSkillScan(resume_id=resume_id)
            self.db.add(row)
        row.text_hash = text_hash
        row.detection_version = detection_version
        row.scanned_unknown_skill_ids = list(scanned_ids)
        row.hits = hits
        self.db.flush()
        return row
