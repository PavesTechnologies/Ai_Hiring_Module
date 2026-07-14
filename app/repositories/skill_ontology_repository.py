from typing import Optional
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, aliased

from app.models.skills import CandidateSkill, SkillOntology

ParentSkill = aliased(SkillOntology)


class SkillOntologyRepository:
    """CRUD and aggregate queries backing the Skill Ontology dashboard/list/detail/update endpoints."""

    def __init__(self, db: Session):
        self.db = db

    def get_dashboard_summary(self) -> dict:
        row = self.db.query(
            func.count(SkillOntology.id).label("total_skills"),
            func.count(SkillOntology.id).filter(SkillOntology.confidence == "verified").label("verified_skills"),
            func.count(SkillOntology.id).filter(SkillOntology.confidence == "unverified").label("unverified_skills"),
            func.count(SkillOntology.id).filter(SkillOntology.is_active.is_(True)).label("active_skills"),
            func.count(SkillOntology.id).filter(SkillOntology.is_active.is_(False)).label("inactive_skills"),
            func.count(func.distinct(SkillOntology.category)).label("categories"),
        ).one()
        return row._asdict()

    def get_categories(self) -> list[tuple[str, int]]:
        return (
            self.db.query(SkillOntology.category, func.count(SkillOntology.id))
            .filter(
                SkillOntology.is_active.is_(True),
                SkillOntology.category.isnot(None),
            )
            .group_by(SkillOntology.category)
            .order_by(SkillOntology.category.asc())
            .all()
        )

    def _apply_filters(
        self,
        query,
        *,
        search: Optional[str],
        category: Optional[str],
        confidence: Optional[str],
        is_active: Optional[bool],
    ):
        if search:
            pattern = f"%{search.strip()}%"
            unnested_aliases = func.unnest(SkillOntology.aliases).table_valued("alias").render_derived()
            alias_match = (
                select(1)
                .select_from(unnested_aliases)
                .where(unnested_aliases.c.alias.ilike(pattern))
                .correlate(SkillOntology)
                .exists()
            )
            query = query.filter(
                or_(
                    SkillOntology.canonical_name.ilike(pattern),
                    SkillOntology.category.ilike(pattern),
                    alias_match,
                )
            )

        if category:
            query = query.filter(SkillOntology.category == category)

        if confidence:
            query = query.filter(SkillOntology.confidence == confidence)

        if is_active is not None:
            query = query.filter(SkillOntology.is_active.is_(is_active))

        return query

    def count_skills(
        self,
        *,
        search: Optional[str] = None,
        category: Optional[str] = None,
        confidence: Optional[str] = None,
        is_active: Optional[bool] = None,
    ) -> int:
        query = self._apply_filters(
            self.db.query(func.count(SkillOntology.id)),
            search=search,
            category=category,
            confidence=confidence,
            is_active=is_active,
        )
        return query.scalar() or 0

    def get_skills(
        self,
        *,
        page: int,
        page_size: int,
        search: Optional[str] = None,
        category: Optional[str] = None,
        confidence: Optional[str] = None,
        is_active: Optional[bool] = None,
    ) -> list[tuple[SkillOntology, Optional[str]]]:
        """Returns (skill, parent_canonical_name) rows, parent resolved via a single outer join."""
        query = self.db.query(SkillOntology, ParentSkill.canonical_name).outerjoin(
            ParentSkill, SkillOntology.parent_skill_id == ParentSkill.id
        )
        query = self._apply_filters(
            query, search=search, category=category, confidence=confidence, is_active=is_active
        )
        return (
            query.order_by(SkillOntology.canonical_name.asc())
            .offset((page - 1) * page_size)
            .limit(page_size)
            .all()
        )

    def get_skill_by_id(self, skill_id: UUID) -> Optional[SkillOntology]:
        return self.db.query(SkillOntology).filter(SkillOntology.id == skill_id).first()

    def get_parent_name(self, parent_skill_id: UUID) -> Optional[str]:
        return (
            self.db.query(SkillOntology.canonical_name)
            .filter(SkillOntology.id == parent_skill_id)
            .scalar()
        )

    def get_children(self, skill_id: UUID) -> list[SkillOntology]:
        return (
            self.db.query(SkillOntology)
            .filter(SkillOntology.parent_skill_id == skill_id)
            .order_by(SkillOntology.canonical_name.asc())
            .all()
        )

    def get_by_canonical_name_exact(self, name: str) -> Optional[SkillOntology]:
        return self.db.query(SkillOntology).filter(SkillOntology.canonical_name == name).first()

    def find_skill_by_alias(self, alias: str, *, exclude_id: UUID) -> Optional[SkillOntology]:
        """Finds another skill (not exclude_id) whose aliases array already contains this exact alias."""
        return (
            self.db.query(SkillOntology)
            .filter(SkillOntology.aliases.any(alias), SkillOntology.id != exclude_id)
            .first()
        )

    def count_candidate_matches_by_alias(self, alias: str) -> int:
        """
        Historical candidate_skills rows that matched this exact alias
        (match_tier='alias'). Read-only impact preview for S04-T02 — never
        used to mutate candidate_skills, which must stay untouched.
        """
        return (
            self.db.query(func.count(CandidateSkill.id))
            .filter(
                func.lower(CandidateSkill.match_tier) == "alias",
                CandidateSkill.raw_extracted_text == alias,
            )
            .scalar()
            or 0
        )

    def get_skills_for_export(
        self,
        *,
        search: Optional[str] = None,
        category: Optional[str] = None,
        confidence: Optional[str] = None,
        is_active: Optional[bool] = None,
    ) -> list[tuple[SkillOntology, Optional[str]]]:
        """Same shape as get_skills(), but unpaginated (every matching row) for Excel export."""
        query = self.db.query(SkillOntology, ParentSkill.canonical_name).outerjoin(
            ParentSkill, SkillOntology.parent_skill_id == ParentSkill.id
        )
        query = self._apply_filters(
            query, search=search, category=category, confidence=confidence, is_active=is_active
        )
        return query.order_by(SkillOntology.canonical_name.asc()).all()

    def get_parents(self, *, search: Optional[str] = None, limit: int = 20) -> list[SkillOntology]:
        query = self.db.query(SkillOntology).filter(SkillOntology.is_active.is_(True))
        if search:
            query = query.filter(SkillOntology.canonical_name.ilike(f"%{search.strip()}%"))
        return query.order_by(SkillOntology.canonical_name.asc()).limit(limit).all()

    def create_skill(self, skill: SkillOntology) -> SkillOntology:
        self.db.add(skill)
        self.db.flush()
        self.db.refresh(skill)
        return skill

    def update_skill(self, skill: SkillOntology) -> SkillOntology:
        self.db.flush()
        self.db.refresh(skill)
        return skill

    def commit(self) -> None:
        self.db.commit()

    def rollback(self) -> None:
        self.db.rollback()
