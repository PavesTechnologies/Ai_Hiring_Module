import time
from uuid import UUID

from app.repositories.candidate_filter_repository import CandidateFilterRepository
from app.repositories.skill_search_repository import SkillSearchRepository
from app.schemas.dashboard.candidate_search_schema import SkillSuggestionResponse


class CandidateSearchService:
    """
    Skill autocomplete, multi-skill AND search, and the resume-derived filters
    for a campaign's candidate list.

    Was SavedViewService until saved views and cross-campaign search were
    removed — the saved-view CRUD is gone and cross-campaign search is covered
    by the Talent Pool module, so what remains is purely candidate search.
    """

    def __init__(
        self,
        skill_search_repo: SkillSearchRepository,
        candidate_filter_repo: CandidateFilterRepository,
    ):
        self.skill_search_repo = skill_search_repo
        self.candidate_filter_repo = candidate_filter_repo

    # ── skill search ──────────────────────────────────────────────────

    def suggest_skills(
        self, campaign_id: UUID, query: str, limit: int = 10
    ) -> list[SkillSuggestionResponse]:
        if not query or not query.strip():
            return []
        return [
            SkillSuggestionResponse(
                canonical_skill_id=r.id,
                canonical_name=r.canonical_name,
                category=r.category,
                candidate_count=r.candidate_count or 0,
            )
            for r in self.skill_search_repo.suggest_skills(campaign_id, query, limit)
        ]

    def resolve_skill_filter(
        self,
        *,
        campaign_id: UUID,
        skill_ids: list[UUID],
        user_id: str | None,
        query_text: str = "",
    ) -> tuple[list[UUID], dict[str, list[str]]]:
        """
        Returns (matching campaign_candidate ids, {cc_id: [match_tier,...]}) and
        logs the search.

        Logging happens after the result count is known so zero-result searches
        are analysable — those are the ones that reveal missing ontology entries.
        """
        started = time.perf_counter()
        ids = self.skill_search_repo.candidate_ids_with_all_skills(campaign_id, skill_ids)

        tiers: dict[str, list[str]] = {}
        for row in self.skill_search_repo.match_tiers_for(ids, skill_ids):
            tier = row.match_tier.value if hasattr(row.match_tier, "value") else str(row.match_tier)
            tiers.setdefault(str(row.cc_id), []).append(tier)

        self.skill_search_repo.log_search(
            user_id=user_id,
            campaign_id=campaign_id,
            query_text=query_text or ",".join(str(s) for s in skill_ids),
            canonical_skill_ids=[str(s) for s in skill_ids],
            result_count=len(ids),
            latency_ms=int((time.perf_counter() - started) * 1000),
            search_type="CAMPAIGN_SKILL_SEARCH",
        )
        return ids, tiers

    # ── resume-derived filters ────────────────────────────────────────

    def filter_candidates(self, campaign_id: UUID, **filters) -> list[UUID] | None:
        return self.candidate_filter_repo.filter_candidate_ids(campaign_id, **filters)

    def get_campaign_uploaders(self, campaign_id: UUID):
        return self.candidate_filter_repo.get_campaign_uploaders(campaign_id)
