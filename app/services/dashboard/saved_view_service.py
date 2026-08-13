import time
from datetime import datetime, timezone
from uuid import UUID

from app.exceptions.campaign_exceptions import CampaignException
from app.models.saved_views import UserSavedView
from app.repositories.candidate_filter_repository import CandidateFilterRepository
from app.repositories.config_repository import ConfigRepository
from app.repositories.saved_view_repository import SavedViewRepository
from app.repositories.skill_search_repository import SkillSearchRepository
from app.models.pipeline import PipelineStage
from app.schemas.dashboard.saved_view_schema import (
    CandidateCampaignAppearanceResponse,
    CrossCampaignCandidateResponse,
    CrossCampaignSearchResponse,
    SavedViewCreateRequest,
    SavedViewResponse,
    SavedViewUpdateRequest,
    SkillSuggestionResponse,
)

DEFAULT_MAX_SAVED_VIEWS = 10


class SavedViewService:
    """Saved views + S01 skill search."""

    def __init__(
        self,
        saved_view_repo: SavedViewRepository,
        config_repo: ConfigRepository,
        skill_search_repo: SkillSearchRepository,
        candidate_filter_repo: "CandidateFilterRepository | None" = None,
    ):
        self.saved_view_repo = saved_view_repo
        self.config_repo = config_repo
        self.skill_search_repo = skill_search_repo
        self.candidate_filter_repo = candidate_filter_repo

    # ── saved views ───────────────────────────────────────────────────

    def _max_views(self) -> int:
        configs = self.config_repo.get_configs_by_keys(["MAX_SAVED_VIEWS_PER_USER"])
        return int(configs.get("MAX_SAVED_VIEWS_PER_USER", str(DEFAULT_MAX_SAVED_VIEWS)))

    @staticmethod
    def _to_response(view: UserSavedView) -> SavedViewResponse:
        return SavedViewResponse(
            id=view.id,
            campaign_id=view.campaign_id,
            name=view.name,
            description=view.description,
            filters=view.filters or {},
            last_applied_at=view.last_applied_at,
            created_at=view.created_at,
            updated_at=view.updated_at,
        )

    def list_views(self, user_id: str, campaign_id: UUID) -> list[SavedViewResponse]:
        return [self._to_response(v) for v in self.saved_view_repo.list_for_user(user_id, campaign_id)]

    def create_view(
        self, user_id: str, campaign_id: UUID, request: SavedViewCreateRequest
    ) -> SavedViewResponse:
        name = request.name.strip()
        if not name:
            raise CampaignException("View name cannot be empty.", 422)

        # Enforced here rather than in the UI — a client-side cap is advisory.
        limit = self._max_views()
        if self.saved_view_repo.count_for_user(user_id, campaign_id) >= limit:
            raise CampaignException(
                f"You already have {limit} saved views for this campaign. "
                f"Delete one before saving another.",
                409,
            )

        if self.saved_view_repo.get_by_name(user_id, campaign_id, name):
            raise CampaignException(f"You already have a view named '{name}'.", 409)

        try:
            view = self.saved_view_repo.add(
                UserSavedView(
                    user_id=user_id,
                    campaign_id=campaign_id,
                    name=name,
                    description=(request.description or "").strip() or None,
                    filters=request.filters or {},
                )
            )
            self.saved_view_repo.commit()
            return self._to_response(view)
        except Exception:
            self.saved_view_repo.rollback()
            raise

    def update_view(
        self, user_id: str, view_id: UUID, request: SavedViewUpdateRequest
    ) -> SavedViewResponse:
        view = self.saved_view_repo.get_owned(view_id, user_id)
        if not view:
            raise CampaignException("Saved view not found.", 404)

        try:
            if request.name is not None:
                name = request.name.strip()
                if not name:
                    raise CampaignException("View name cannot be empty.", 422)
                clash = self.saved_view_repo.get_by_name(user_id, view.campaign_id, name)
                if clash and clash.id != view.id:
                    raise CampaignException(f"You already have a view named '{name}'.", 409)
                view.name = name
            if request.description is not None:
                view.description = request.description.strip() or None
            if request.filters is not None:
                view.filters = request.filters
            view.updated_at = datetime.now(timezone.utc)
            self.saved_view_repo.commit()
            return self._to_response(view)
        except Exception:
            self.saved_view_repo.rollback()
            raise

    def mark_applied(self, user_id: str, view_id: UUID) -> SavedViewResponse:
        """Records last_applied_at so the manage panel can show staleness (T02)."""
        view = self.saved_view_repo.get_owned(view_id, user_id)
        if not view:
            raise CampaignException("Saved view not found.", 404)
        try:
            view.last_applied_at = datetime.now(timezone.utc)
            self.saved_view_repo.commit()
            return self._to_response(view)
        except Exception:
            self.saved_view_repo.rollback()
            raise

    def delete_view(self, user_id: str, view_id: UUID) -> None:
        view = self.saved_view_repo.get_owned(view_id, user_id)
        if not view:
            raise CampaignException("Saved view not found.", 404)
        try:
            self.saved_view_repo.delete(view)
            self.saved_view_repo.commit()
        except Exception:
            self.saved_view_repo.rollback()
            raise

    # ── skill search ────────────────────────────────────────────

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
        logs the search (T03).

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

    # ── cross-campaign search ───────────────────────────────────

    def cross_campaign_search(
        self,
        *,
        skill_ids: list[UUID],
        user_id: str | None,
        accessible_campaign_ids=None,
        min_composite_score: float | None = None,
        campaign_statuses: list | None = None,
        reached_stage=None,
        rejected_only: bool = False,
        query_text: str = "",
    ) -> CrossCampaignSearchResponse:
        started = time.perf_counter()
        rows = self.skill_search_repo.cross_campaign_candidates(
            skill_ids=skill_ids,
            accessible_campaign_ids=accessible_campaign_ids,
            min_composite_score=min_composite_score,
            campaign_statuses=campaign_statuses,
            reached_stage=reached_stage,
        )

        # collapse appearances into one entry per candidate
        by_candidate: dict[str, list] = {}
        for r in rows:
            by_candidate.setdefault(str(r.candidate_id), []).append(r)

        results: list[CrossCampaignCandidateResponse] = []
        for candidate_id, appearances in by_candidate.items():
            mapped = [
                CandidateCampaignAppearanceResponse(
                    campaign_id=a.campaign_id,
                    campaign_candidate_id=a.campaign_candidate_id,
                    campaign_name=a.campaign_name,
                    campaign_status=a.campaign_status.value
                    if hasattr(a.campaign_status, "value") else str(a.campaign_status),
                    jd_title=a.jd_title,
                    pipeline_stage=a.pipeline_stage.value
                    if hasattr(a.pipeline_stage, "value") else str(a.pipeline_stage),
                    composite_score=float(a.composite_score) if a.composite_score is not None else None,
                )
                for a in appearances
            ]
            scored = [m for m in mapped if m.composite_score is not None]
            best = max(scored, key=lambda m: m.composite_score) if scored else None
            rejected_everywhere = all(m.pipeline_stage == PipelineStage.REJECTED.value for m in mapped)

            if rejected_only and not rejected_everywhere:
                continue

            results.append(
                CrossCampaignCandidateResponse(
                    candidate_id=candidate_id,
                    best_composite_score=best.composite_score if best else None,
                    best_campaign_name=best.campaign_name if best else None,
                    appearances=mapped,
                    rejected_everywhere=rejected_everywhere,
                )
            )

        # best score first; unscored candidates last rather than treated as 0
        results.sort(key=lambda r: (r.best_composite_score is None, -(r.best_composite_score or 0)))

        self.skill_search_repo.log_search(
            user_id=user_id,
            campaign_id=None,          # spans campaigns by definition
            query_text=query_text or ",".join(str(s) for s in skill_ids),
            canonical_skill_ids=[str(s) for s in skill_ids],
            result_count=len(results),
            latency_ms=int((time.perf_counter() - started) * 1000),
            search_type="CROSS_CAMPAIGN",
        )
        return CrossCampaignSearchResponse(results=results, result_count=len(results))

    # ── resume-derived filters ──────────────────────────

    def filter_candidates(self, campaign_id: UUID, **filters) -> list[UUID] | None:
        if self.candidate_filter_repo is None:
            return None
        return self.candidate_filter_repo.filter_candidate_ids(campaign_id, **filters)

    def get_campaign_uploaders(self, campaign_id: UUID):
        if self.candidate_filter_repo is None:
            return []
        return self.candidate_filter_repo.get_campaign_uploaders(campaign_id)
