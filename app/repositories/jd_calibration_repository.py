"""Queries for JD calibration (Phase 2)."""
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.campaigns import HiringCampaign
from app.models.identity import User
from app.models.jd.job_descriptions import JDCalibrationVersion, JobDescription
from app.models.skills import JDSkill, JDUnknownSkill, SkillOntology, UnknownSkill
from app.repositories.skill_detection_repository import SkillDetectionRepository


class JDCalibrationRepository:
    def __init__(self, db: Session):
        self.db = db

    def get_jd(self, jd_id: UUID) -> JobDescription | None:
        return self.db.get(JobDescription, jd_id)

    def jd_skills_with_names(self, jd_id: UUID) -> list[tuple[JDSkill, str]]:
        rows = self.db.execute(
            select(JDSkill, SkillOntology.canonical_name)
            .join(SkillOntology, SkillOntology.id == JDSkill.canonical_skill_id)
            .where(JDSkill.jd_id == jd_id)
            .order_by(SkillOntology.canonical_name)
        ).all()
        return [(skill, name) for skill, name in rows]

    def unknown_skill_links(self, jd_id: UUID) -> list[tuple[JDUnknownSkill, UnknownSkill]]:
        rows = self.db.execute(
            select(JDUnknownSkill, UnknownSkill)
            .join(UnknownSkill, UnknownSkill.id == JDUnknownSkill.unknown_skill_id)
            .where(JDUnknownSkill.jd_id == jd_id)
            .order_by(UnknownSkill.raw_text)
        ).all()
        return [(link, unknown) for link, unknown in rows]

    def load_senses(self):
        return SkillDetectionRepository(self.db).load_senses()

    def campaigns_for_jd(self, jd_id: UUID) -> list[HiringCampaign]:
        return self.db.execute(select(HiringCampaign).where(HiringCampaign.jd_id == jd_id)).scalars().all()

    def user_names(self, user_ids: list[str]) -> dict[str, str]:
        ids = [uid for uid in set(user_ids) if uid]
        if not ids:
            return {}
        return dict(self.db.execute(select(User.id, User.full_name).where(User.id.in_(ids))).all())

    def latest_version_no(self, jd_id: UUID) -> int:
        row = self.db.execute(
            select(JDCalibrationVersion.version_no).where(JDCalibrationVersion.jd_id == jd_id)
            .order_by(JDCalibrationVersion.version_no.desc()).limit(1)
        ).first()
        return row[0] if row else 0

    def skill_names(self, skill_ids) -> dict:
        ids = [i for i in dict.fromkeys(skill_ids) if i]
        if not ids:
            return {}
        return dict(self.db.execute(
            select(SkillOntology.id, SkillOntology.canonical_name).where(SkillOntology.id.in_(ids))).all())

    def latest_version(self, jd_id: UUID) -> JDCalibrationVersion | None:
        return self.db.execute(
            select(JDCalibrationVersion).where(JDCalibrationVersion.jd_id == jd_id)
            .order_by(JDCalibrationVersion.version_no.desc()).limit(1)
        ).scalars().first()

    def add_version(self, version: JDCalibrationVersion) -> None:
        self.db.add(version)

    def list_versions(self, jd_id: UUID) -> list[JDCalibrationVersion]:
        return self.db.execute(
            select(JDCalibrationVersion).where(JDCalibrationVersion.jd_id == jd_id)
            .order_by(JDCalibrationVersion.version_no.desc())
        ).scalars().all()

    def flush(self) -> None:
        self.db.flush()

    def commit(self) -> None:
        self.db.commit()

    def rollback(self) -> None:
        self.db.rollback()
