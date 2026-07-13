from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.skills import (
    JDSkill,
    JDSkillVerificationStatus,
    JDUnknownSkill,
    JDUnknownSkillStatus,
    SkillOntology,
    UnknownSkill,
    UnknownSkillStatus,
)

# Semantic Canonical tier only fires below the fuzzy threshold, so this can
# be looser than fuzzy's 85% — it's the last chance to avoid an UNKNOWN
# before falling through, not a replacement for the deterministic tiers.
SEMANTIC_SIMILARITY_THRESHOLD = 0.80


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

    def upsert_unknown_skill(self, raw_text: str, normalized_key: str | None = None) -> UnknownSkill:
        # Dedup stays keyed on raw_text (unchanged) — normalized_key is
        # informational only (e.g. for grouping near-duplicates in the HR
        # review queue), not a new uniqueness rule.
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

        unknown_skill = UnknownSkill(raw_text=raw_text, normalized_key=normalized_key)
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
        verification_status: JDSkillVerificationStatus,
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
            verification_status=verification_status,
        )
        self.db.add(jd_skill)
        self.db.flush()
        self.db.refresh(jd_skill)
        return jd_skill

    def get_jd_skill(self, jd_id: UUID, canonical_skill_id: UUID) -> JDSkill | None:
        return (
            self.db.query(JDSkill)
            .filter(JDSkill.jd_id == jd_id, JDSkill.canonical_skill_id == canonical_skill_id)
            .first()
        )

    def get_jd_skill_by_id(self, jd_skill_id: UUID) -> JDSkill | None:
        return self.db.query(JDSkill).filter(JDSkill.id == jd_skill_id).first()

    def remap_jd_skill(self, jd_skill: JDSkill, new_canonical_skill_id: UUID) -> JDSkill:
        """
        HR-driven override of an existing JDSkill's canonical mapping.
        Updates canonical_skill_id in place (no history column, per the
        finalized design) and marks the row as a human decision rather than
        leaving a stale AI-matching tier/confidence in place.
        """
        jd_skill.canonical_skill_id = new_canonical_skill_id
        jd_skill.match_tier = "MANUAL_HR"
        jd_skill.confidence = 1.0
        jd_skill.verification_status = JDSkillVerificationStatus.AUTO_VERIFIED
        self.db.flush()
        self.db.refresh(jd_skill)
        return jd_skill

    def link_unknown_skill_to_jd(
        self,
        jd_id: UUID,
        unknown_skill_id: UUID,
        mandatory: bool | None = None,
    ) -> JDUnknownSkill:
        existing = (
            self.db.query(JDUnknownSkill)
            .filter(JDUnknownSkill.jd_id == jd_id, JDUnknownSkill.unknown_skill_id == unknown_skill_id)
            .first()
        )
        if existing:
            return existing

        link = JDUnknownSkill(jd_id=jd_id, unknown_skill_id=unknown_skill_id, mandatory=mandatory)
        self.db.add(link)
        self.db.flush()
        self.db.refresh(link)
        return link

    def find_by_embedding(self, embedding: list[float]) -> tuple[SkillOntology, float] | None:
        """
        Semantic Canonical tier: nearest active canonical skill by cosine
        distance. Canonical-name matching only — aliases are intentionally
        excluded from fuzzy/semantic search per the finalized pipeline.
        Returns (skill, similarity) where similarity = 1 - cosine_distance,
        or None if no active skill has an embedding at all.
        """
        distance = SkillOntology.embedding.cosine_distance(embedding)
        result = (
            self.db.query(SkillOntology, distance.label("distance"))
            .filter(SkillOntology.is_active.is_(True), SkillOntology.embedding.isnot(None))
            .order_by(distance)
            .first()
        )
        if result is None:
            return None
        skill, dist = result
        return skill, 1.0 - dist

    def find_skill_by_name_or_alias(self, text: str) -> SkillOntology | None:
        """
        Case-insensitive lookup used to guard alias uniqueness: a candidate
        alias must not already be a canonical name, and must not already
        belong to a different canonical skill's alias list.
        """
        lowered = text.lower()
        return (
            self.db.query(SkillOntology)
            .filter(
                (func.lower(SkillOntology.canonical_name) == lowered)
                | SkillOntology.aliases.any(text)
            )
            .first()
        )

    def append_alias(self, skill: SkillOntology, alias: str) -> SkillOntology:
        skill.aliases = [*(skill.aliases or []), alias]
        self.db.flush()
        self.db.refresh(skill)
        return skill

    def create_skill_ontology(
        self,
        canonical_name: str,
        source: str,
        category: str | None = None,
    ) -> SkillOntology:
        skill = SkillOntology(
            canonical_name=canonical_name,
            source=source,
            category=category,
        )
        self.db.add(skill)
        self.db.flush()
        self.db.refresh(skill)
        return skill

    def get_skill_by_id(self, skill_id: UUID) -> SkillOntology | None:
        return self.db.query(SkillOntology).filter(SkillOntology.id == skill_id).first()

    def get_unknown_skill_by_id(self, unknown_skill_id: UUID) -> UnknownSkill | None:
        return self.db.query(UnknownSkill).filter(UnknownSkill.id == unknown_skill_id).first()

    def get_pending_unknown_skills(self) -> list[UnknownSkill]:
        """HR review queue, highest-frequency (most impactful to resolve) first."""
        return (
            self.db.query(UnknownSkill)
            .filter(
                UnknownSkill.status.in_(
                    [UnknownSkillStatus.PENDING, UnknownSkillStatus.UNDER_REVIEW]
                )
            )
            .order_by(UnknownSkill.frequency.desc())
            .all()
        )

    def update_unknown_skill_status(
        self, unknown_skill: UnknownSkill, status: UnknownSkillStatus
    ) -> UnknownSkill:
        unknown_skill.status = status
        self.db.flush()
        self.db.refresh(unknown_skill)
        return unknown_skill

    def get_pending_jd_links(self, unknown_skill_id: UUID) -> list[JDUnknownSkill]:
        """
        Every not-yet-resolved JDUnknownSkill link for an UnknownSkill —
        the set of JDs that need a retroactive JDSkill row once it's
        mapped/promoted. Filtered to PENDING so re-running a resolution
        action is idempotent (already-resolved links are skipped).
        """
        return (
            self.db.query(JDUnknownSkill)
            .filter(
                JDUnknownSkill.unknown_skill_id == unknown_skill_id,
                JDUnknownSkill.status == JDUnknownSkillStatus.PENDING,
            )
            .all()
        )

    def mark_jd_unknown_skill_resolved(self, link: JDUnknownSkill) -> JDUnknownSkill:
        link.status = JDUnknownSkillStatus.RESOLVED
        self.db.flush()
        self.db.refresh(link)
        return link

    def commit(self) -> None:
        self.db.commit()

    def rollback(self) -> None:
        self.db.rollback()
