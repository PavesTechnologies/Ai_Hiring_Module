from uuid import UUID

from sqlalchemy import String, cast, func, or_, select
from sqlalchemy.orm import Session

from app.models.pipeline import CampaignCandidate
from app.models.search import SearchQuery
from app.models.skills import CandidateSkill, SkillOntology


class SkillSearchRepository:
    """M11-E03-S01 — skill autocomplete and multi-skill candidate filtering."""

    def __init__(self, db: Session):
        self.db = db

    def suggest_skills(self, campaign_id: UUID, query: str, limit: int = 10):
        """
        Autocomplete over canonical_name AND aliases, restricted to skills that
        actually appear on candidates in THIS campaign.

        Suggesting skills nobody in the campaign has would produce guaranteed
        zero-result searches, so the candidate_skills join is the point of the
        query, not an optimisation.
        """
        pattern = f"%{query.strip()}%"

        # unnest aliases so an alias match ("JS") surfaces its canonical skill
        alias_hit = (
            select(1)
            .select_from(func.unnest(SkillOntology.aliases).alias("alias"))
            .where(func.lower(cast(func.unnest(SkillOntology.aliases), String)).like(pattern.lower()))
            .exists()
        )

        return (
            self.db.query(
                SkillOntology.id,
                SkillOntology.canonical_name,
                SkillOntology.category,
                func.count(func.distinct(CandidateSkill.candidate_id)).label("candidate_count"),
            )
            .join(CandidateSkill, CandidateSkill.canonical_skill_id == SkillOntology.id)
            .join(CampaignCandidate, CampaignCandidate.resume_id == CandidateSkill.resume_id)
            .filter(
                CampaignCandidate.campaign_id == campaign_id,
                CandidateSkill.scoring_weight > 0,
                SkillOntology.is_active.is_(True),
                or_(SkillOntology.canonical_name.ilike(pattern), alias_hit),
            )
            .group_by(SkillOntology.id, SkillOntology.canonical_name, SkillOntology.category)
            .order_by(func.count(func.distinct(CandidateSkill.candidate_id)).desc())
            .limit(limit)
            .all()
        )

    def candidate_ids_with_all_skills(self, campaign_id: UUID, skill_ids: list[UUID]) -> list[UUID]:
        """
        campaign_candidate ids whose candidate holds EVERY requested skill.

        AND logic is enforced with GROUP BY ... HAVING COUNT(DISTINCT skill) =
        len(skill_ids). A plain IN() would return candidates matching ANY of
        them, which is the classic wrong answer for "Python AND AWS".
        """
        if not skill_ids:
            return []

        rows = (
            self.db.query(CampaignCandidate.id)
            .join(CandidateSkill, CandidateSkill.resume_id == CampaignCandidate.resume_id)
            .filter(
                CampaignCandidate.campaign_id == campaign_id,
                CandidateSkill.canonical_skill_id.in_(skill_ids),
                CandidateSkill.scoring_weight > 0,
            )
            .group_by(CampaignCandidate.id)
            .having(func.count(func.distinct(CandidateSkill.canonical_skill_id)) == len(skill_ids))
            .all()
        )
        return [r.id for r in rows]

    def match_tiers_for(self, campaign_candidate_ids: list[UUID], skill_ids: list[UUID]):
        """
        (campaign_candidate_id, canonical_skill_id, match_tier) so each row can
        show HOW every searched skill matched, per T01/T02.
        """
        if not campaign_candidate_ids or not skill_ids:
            return []
        return (
            self.db.query(
                CampaignCandidate.id.label("cc_id"),
                CandidateSkill.canonical_skill_id,
                CandidateSkill.match_tier,
            )
            .join(CandidateSkill, CandidateSkill.resume_id == CampaignCandidate.resume_id)
            .filter(
                CampaignCandidate.id.in_(campaign_candidate_ids),
                CandidateSkill.canonical_skill_id.in_(skill_ids),
            )
            .all()
        )

    def log_search(
        self,
        *,
        user_id: str | None,
        campaign_id: UUID | None,
        query_text: str,
        canonical_skill_ids: list[str],
        result_count: int,
        latency_ms: int | None,
        search_type: str,
    ) -> None:
        """
        T03 analytics. Never raises: a logging failure must not fail the search
        the user actually asked for.
        """
        try:
            self.db.add(
                SearchQuery(
                    queried_by=user_id,
                    campaign_id=campaign_id,
                    query_text=query_text,
                    canonical_skill_ids=canonical_skill_ids,
                    result_count=result_count,
                    latency_ms=latency_ms,
                    search_type=search_type,
                )
            )
            self.db.commit()
        except Exception:
            self.db.rollback()
