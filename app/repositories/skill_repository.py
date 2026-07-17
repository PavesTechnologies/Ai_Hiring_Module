from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import and_, func
from sqlalchemy import text as sql_text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.skills import (
    CandidateSkill,
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

    def upsert_unknown_skill(
        self, raw_text: str, normalized_key: str | None = None
    ) -> tuple[UnknownSkill, bool]:
        """
        Returns (unknown_skill, was_created) — was_created distinguishes a
        brand-new row from a frequency-bump of an existing one, so the
        caller can audit-log creation exactly once.

        Dedup stays keyed on raw_text (unchanged) — normalized_key is
        informational only (e.g. for grouping near-duplicates in the HR
        review queue), not a new uniqueness rule.
        """
        existing = (
            self.db.query(UnknownSkill)
            .filter(UnknownSkill.raw_text == raw_text)
            .first()
        )
        if existing:
            existing.frequency += 1
            existing.last_seen = datetime.now(timezone.utc)
            self.db.flush()
            return existing, False

        # Two JDs processed concurrently can both see "no existing row" for
        # the same never-before-seen raw_text and both attempt an insert —
        # raw_text is unique, so the loser's flush raises IntegrityError.
        # A SAVEPOINT scopes the rollback to just this insert attempt,
        # leaving the rest of this (much larger) persistence transaction
        # untouched, then falls back to the row the winner just committed.
        try:
            with self.db.begin_nested():
                unknown_skill = UnknownSkill(raw_text=raw_text, normalized_key=normalized_key)
                self.db.add(unknown_skill)
                self.db.flush()
        except IntegrityError:
            existing = (
                self.db.query(UnknownSkill)
                .filter(UnknownSkill.raw_text == raw_text)
                .first()
            )
            existing.frequency += 1
            existing.last_seen = datetime.now(timezone.utc)
            self.db.flush()
            return existing, False

        self.db.refresh(unknown_skill)
        return unknown_skill, True

    def _apply_unknown_skill_filters(self, query, *, search: str | None, status: str | None):
        if search:
            query = query.filter(UnknownSkill.raw_text.ilike(f"%{search.strip()}%"))
        if status:
            query = query.filter(UnknownSkill.status == status)
        return query

    def count_unknown_skills(self, *, search: str | None = None, status: str | None = None) -> int:
        query = self._apply_unknown_skill_filters(
            self.db.query(func.count(UnknownSkill.id)), search=search, status=status
        )
        return query.scalar() or 0

    def list_unknown_skills(
        self,
        *,
        page: int,
        page_size: int,
        search: str | None = None,
        status: str | None = None,
    ) -> list[UnknownSkill]:
        query = self._apply_unknown_skill_filters(self.db.query(UnknownSkill), search=search, status=status)
        return (
            query.order_by(UnknownSkill.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
            .all()
        )

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

    def get_jd_skills_by_jd_id(self, jd_id: UUID) -> list[tuple[JDSkill, SkillOntology]]:
        """Resolved (canonical) skills for a JD, paired with their SkillOntology row for display."""
        return (
            self.db.query(JDSkill, SkillOntology)
            .join(SkillOntology, JDSkill.canonical_skill_id == SkillOntology.id)
            .filter(JDSkill.jd_id == jd_id)
            .order_by(JDSkill.created_at)
            .all()
        )

    def get_jd_unknown_skills_by_jd_id(self, jd_id: UUID) -> list[tuple[JDUnknownSkill, UnknownSkill]]:
        """Not-yet-resolved (or resolved) unknown-skill occurrences for a JD, paired with the deduped UnknownSkill row."""
        return (
            self.db.query(JDUnknownSkill, UnknownSkill)
            .join(UnknownSkill, JDUnknownSkill.unknown_skill_id == UnknownSkill.id)
            .filter(JDUnknownSkill.jd_id == jd_id)
            .order_by(JDUnknownSkill.created_at)
            .all()
        )

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

    def find_best_semantic_match(
        self, target_embedding: list[float], candidate_skill_ids: list[UUID]
    ) -> tuple[UUID, float] | None:
        """
        Deterministic-scoring SEMANTIC tier (M07-E01 S02): highest cosine
        similarity between target_embedding (a missing mandatory skill's
        embedding) and just the candidate's own canonical skill ids -
        unlike find_by_embedding, this never searches the whole catalog.
        Skills with no embedding yet (still queued) are excluded rather
        than compared. Returns (candidate_skill_id, similarity) or None if
        candidate_skill_ids is empty or none of them have an embedding.
        """
        if not candidate_skill_ids:
            return None

        distance = SkillOntology.embedding.cosine_distance(target_embedding)
        result = (
            self.db.query(SkillOntology.id, distance.label("distance"))
            .filter(
                SkillOntology.id.in_(candidate_skill_ids),
                SkillOntology.embedding.isnot(None),
            )
            .order_by(distance)
            .first()
        )
        if result is None:
            return None
        skill_id, dist = result
        return skill_id, 1.0 - dist

    def find_skill_by_name_or_alias(self, text: str) -> SkillOntology | None:
        """
        Case-insensitive lookup used to guard alias uniqueness: a candidate
        alias must not already be a canonical name, and must not already
        belong to a different canonical skill's alias list — "ReactJS" and
        "reactjs" must be treated as the same alias.

        Postgres ARRAY columns have no case-insensitive containment
        operator, so this compares in Python over the full ontology table —
        the same approach the matching pipeline's own case-insensitive tier
        already uses, and the table is small enough that this is cheap; the
        prior SQL-only version compared canonical_name case-insensitively
        but aliases case-sensitively, letting a case-variant duplicate slip
        through.
        """
        lowered = text.lower()
        for skill in self.db.query(SkillOntology).all():
            if skill.canonical_name.lower() == lowered:
                return skill
            if any((alias or "").lower() == lowered for alias in (skill.aliases or [])):
                return skill
        return None

    def acquire_alias_lock(self, alias: str) -> None:
        """
        Transaction-scoped advisory lock keyed by the lowered alias text.
        Aliases have no DB-level uniqueness constraint of their own (ARRAY
        column), so two concurrent "add this alias" calls targeting
        different canonical skills would otherwise both pass a
        find_skill_by_name_or_alias check before either commits. Callers
        must acquire this before re-validating and appending; it releases
        automatically at commit/rollback, no explicit unlock needed.
        """
        self.db.execute(
            sql_text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
            {"key": alias.lower()},
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

    def get_mandatory_jd_skills(
        self,
        jd_id: UUID,
    ) -> list[JDSkill]:
        """
        Return all mandatory canonical skills required by a JD.
        Used by deterministic candidate scoring.
        """
        return (
            self.db.query(JDSkill)
            .filter(
                JDSkill.jd_id == jd_id,
                JDSkill.mandatory.is_(True),
            )
            .all()
        )

    def get_candidate_normalized_skills(
        self,
        resume_id: UUID,
    ) -> list[CandidateSkill]:
        """
        Return candidate skills that were successfully normalized
        to a canonical SkillOntology entry.

        Skills with canonical_skill_id = NULL are excluded because
        they cannot participate in deterministic canonical matching.
        """
        return (
            self.db.query(CandidateSkill)
            .filter(
                CandidateSkill.resume_id == resume_id,
                CandidateSkill.canonical_skill_id.isnot(None),
            )
            .all()
        )

    def get_mandatory_skill_coverage(self, jd_id: UUID, resume_id: UUID):
        """
        One row per mandatory JD skill, LEFT JOINed against the candidate's
        matching normalized skill (if any) - unmatched mandatory skills still
        come back as a row (with NULL candidate_scoring_weight/match_tier/
        confidence) instead of being silently dropped, so this is the single
        source of truth for building a per-mandatory-skill coverage
        breakdown (M07-E01 S02 T01).

        The join condition matches on canonical_skill_id only - never on
        raw_extracted_text - and only considers a candidate skill "in play"
        if its scoring_weight is > 0.
        """
        return (
            self.db.query(
                JDSkill.canonical_skill_id.label("canonical_skill_id"),
                JDSkill.weight.label("weight"),
                JDSkill.mandatory.label("mandatory"),
                CandidateSkill.scoring_weight.label("candidate_scoring_weight"),
                CandidateSkill.match_tier.label("match_tier"),
                CandidateSkill.confidence.label("confidence"),
            )
            .outerjoin(
                CandidateSkill,
                and_(
                    CandidateSkill.canonical_skill_id == JDSkill.canonical_skill_id,
                    CandidateSkill.resume_id == resume_id,
                    CandidateSkill.scoring_weight > 0,
                ),
            )
            .filter(
                JDSkill.jd_id == jd_id,
                JDSkill.mandatory.is_(True),
            )
            .all()
        )
