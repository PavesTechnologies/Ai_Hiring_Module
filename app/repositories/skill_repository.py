from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.skills import JDSkill, JDUnknownSkill, SkillOntology, UnknownSkill


class SkillRepository:
    """
    CRUD for the skill ontology table family (app/models/skills.py).
    Shared across document types — JD today, Resume/CandidateSkill later —
    since skill matching against SkillOntology is not JD-specific.
    """

    def __init__(self, db: Session):
        self.db = db

    def list_active_skills(self) -> list[SkillOntology]:
        return (
            self.db.query(SkillOntology)
            .filter(SkillOntology.is_active.is_(True))
            .all()
        )

    def get_by_canonical_name_exact(self, name: str) -> SkillOntology | None:
        return (
            self.db.query(SkillOntology)
            .filter(SkillOntology.canonical_name == name)
            .first()
        )

    def get_by_canonical_name_ilike(self, name: str) -> SkillOntology | None:
        return (
            self.db.query(SkillOntology)
            .filter(func.lower(SkillOntology.canonical_name) == name.lower())
            .first()
        )

    def upsert_unknown_skill(self, raw_text: str) -> UnknownSkill:
        existing = (
            self.db.query(UnknownSkill)
            .filter(UnknownSkill.raw_text == raw_text)
            .first()
        )
        if existing:
            existing.frequency += 1
            existing.last_seen = datetime.now(timezone.utc)
            self.db.flush()
            return existing

        unknown_skill = UnknownSkill(raw_text=raw_text)
        self.db.add(unknown_skill)
        self.db.flush()
        self.db.refresh(unknown_skill)
        return unknown_skill

    def bump_occurrence_count(self, skill_id: UUID) -> None:
        (
            self.db.query(SkillOntology)
            .filter(SkillOntology.id == skill_id)
            .update({SkillOntology.occurrence_count: SkillOntology.occurrence_count + 1,
                     SkillOntology.last_seen_at: datetime.now(timezone.utc)})
        )

    def create_jd_skill(
        self,
        jd_id: UUID,
        canonical_skill_id: UUID,
        mandatory: bool,
        match_tier: str,
        confidence: float | None = None,
        weight: float | None = None,
    ) -> JDSkill:
        jd_skill = JDSkill(
            jd_id=jd_id,
            canonical_skill_id=canonical_skill_id,
            mandatory=mandatory,
            weight=weight,
            confidence=confidence,
            match_tier=match_tier,
        )
        self.db.add(jd_skill)
        self.db.flush()
        self.db.refresh(jd_skill)
        return jd_skill

    def link_unknown_skill_to_jd(self, jd_id: UUID, unknown_skill_id: UUID) -> JDUnknownSkill:
        existing = (
            self.db.query(JDUnknownSkill)
            .filter(JDUnknownSkill.jd_id == jd_id, JDUnknownSkill.unknown_skill_id == unknown_skill_id)
            .first()
        )
        if existing:
            return existing

        link = JDUnknownSkill(jd_id=jd_id, unknown_skill_id=unknown_skill_id)
        self.db.add(link)
        self.db.flush()
        self.db.refresh(link)
        return link

    def commit(self) -> None:
        self.db.commit()

    def rollback(self) -> None:
        self.db.rollback()
