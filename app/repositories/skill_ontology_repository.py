from typing import Optional
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, aliased

from app.models.skills import CandidateSkill, JDSkill, SkillOntology

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

    def get_all_canonical_names(self) -> dict[str, SkillOntology]:
        """
        canonical_name -> SkillOntology for every skill, loaded once.
        Bulk import (S07-T01/T02) needs this to validate/resolve duplicates
        and parent references across a whole file without a query per row —
        the same "load once" approach SkillSeedService already uses.
        """
        return {skill.canonical_name: skill for skill in self.db.query(SkillOntology).all()}

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

    def get_parents(
        self,
        *,
        search: Optional[str] = None,
        exclude_ids: Optional[set[UUID]] = None,
        limit: int = 20,
    ) -> list[SkillOntology]:
        query = self.db.query(SkillOntology).filter(SkillOntology.is_active.is_(True))
        if search:
            query = query.filter(SkillOntology.canonical_name.ilike(f"%{search.strip()}%"))
        if exclude_ids:
            query = query.filter(SkillOntology.id.notin_(exclude_ids))
        return query.order_by(SkillOntology.canonical_name.asc()).limit(limit).all()

    def get_descendant_ids(self, skill_id: UUID) -> set[UUID]:
        """
        Every descendant of skill_id (children, grandchildren, ...), via a
        level-by-level BFS — one query per tree depth, not per node.
        Used to keep a skill's own descendants out of its parent-search
        results (S05-T01).
        """
        descendant_ids: set[UUID] = set()
        frontier = [skill_id]
        while frontier:
            rows = (
                self.db.query(SkillOntology.id)
                .filter(SkillOntology.parent_skill_id.in_(frontier))
                .all()
            )
            frontier = [row.id for row in rows if row.id not in descendant_ids]
            descendant_ids.update(frontier)
        return descendant_ids

    def _with_has_children(self, query):
        HasChildProbe = aliased(SkillOntology)
        has_children = (
            select(1)
            .select_from(HasChildProbe)
            .where(HasChildProbe.parent_skill_id == SkillOntology.id)
            .correlate(SkillOntology)
            .exists()
        )
        return query.add_columns(has_children.label("has_children"))

    def get_root_skills(self) -> list[tuple[SkillOntology, bool]]:
        """Skills with no parent (parent_skill_id IS NULL), for the hierarchy tree's top level."""
        query = self._with_has_children(self.db.query(SkillOntology)).filter(
            SkillOntology.parent_skill_id.is_(None)
        )
        return query.order_by(SkillOntology.canonical_name.asc()).all()

    def get_children_with_has_children(self, skill_id: UUID) -> list[tuple[SkillOntology, bool]]:
        """Immediate children only — the hierarchy tree lazy-loads one level per expand, never the whole subtree."""
        query = self._with_has_children(self.db.query(SkillOntology)).filter(
            SkillOntology.parent_skill_id == skill_id
        )
        return query.order_by(SkillOntology.canonical_name.asc()).all()

    def count_candidate_usage(self, skill_id: UUID) -> int:
        """S06-T01: candidate_skills rows currently pointing at this canonical skill."""
        return (
            self.db.query(func.count(CandidateSkill.id))
            .filter(CandidateSkill.canonical_skill_id == skill_id)
            .scalar()
            or 0
        )

    def count_jd_usage(self, skill_id: UUID) -> int:
        """S06-T01: jd_skills rows currently pointing at this canonical skill."""
        return (
            self.db.query(func.count(JDSkill.id))
            .filter(JDSkill.canonical_skill_id == skill_id)
            .scalar()
            or 0
        )

    def reparent_children(self, old_parent_id: UUID, new_parent_id: Optional[UUID]) -> int:
        """
        S06-T02: bulk-reassigns every direct child of old_parent_id to
        new_parent_id (or NULL to make them root skills) in a single UPDATE.
        Returns the number of children affected.
        """
        return (
            self.db.query(SkillOntology)
            .filter(SkillOntology.parent_skill_id == old_parent_id)
            .update({SkillOntology.parent_skill_id: new_parent_id}, synchronize_session=False)
        )

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
