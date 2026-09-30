from typing import Any, Iterable

from app.core.cache_keys import (
    campaign_key,
    campaign_list_prefix,
    campaign_scoring_key,
    campaign_weight_presets_key,
    candidate_list_prefix,
    jd_key,
    jd_list_prefix,
    jd_search_prefix,
    resume_key,
    resume_list_prefix,
    skill_alias_catalog_key,
    skill_catalog_key,
    skill_categories_key,
    skill_dashboard_summary_key,
    skill_key,
    skill_prefix,
)
from app.services.cache_service import CacheService


class CacheInvalidator:
    """
    The single source of truth for which cached views a domain change makes
    stale. Services and Celery tasks call the method for what changed rather
    than deleting keys themselves, so a new cached view only has to be
    registered here. A None cache_service (no Redis wired, e.g. unit tests)
    makes every method a no-op.
    """

    def __init__(self, cache_service: CacheService | None):
        self.cache = cache_service

    @classmethod
    def default(cls) -> "CacheInvalidator":
        """For Celery tasks and other code with no injected CacheService."""
        from app.core.redis_client import get_redis_client

        return cls(CacheService(get_redis_client()))

    def jd(self, jd_id: Any = None) -> None:
        if not self.cache:
            return
        if jd_id is not None:
            self.cache.delete(jd_key(jd_id))
        self.cache.delete_by_prefix(jd_list_prefix())
        self.cache.delete_by_prefix(jd_search_prefix())

    def jds(self, jd_ids: Iterable[Any]) -> None:
        jd_ids = list(jd_ids)
        if not self.cache or not jd_ids:
            return
        self.cache.delete(*(jd_key(jd_id) for jd_id in jd_ids))
        self.cache.delete_by_prefix(jd_list_prefix())
        self.cache.delete_by_prefix(jd_search_prefix())

    def resumes(self, resume_ids: Iterable[Any] = ()) -> None:
        """Resume detail + resume/candidate lists (these show parse status and pipeline stage)."""
        if not self.cache:
            return
        resume_ids = list(resume_ids)
        if resume_ids:
            self.cache.delete(*(resume_key(resume_id) for resume_id in resume_ids))
        self.cache.delete_by_prefix(resume_list_prefix())
        self.cache.delete_by_prefix(candidate_list_prefix())

    def campaign(self, campaign_id: Any = None, org_id: Any = None) -> None:
        """Campaign detail/scoring config + every campaign list."""
        if not self.cache:
            return
        if campaign_id is not None:
            self.cache.delete(campaign_key(campaign_id), campaign_scoring_key(campaign_id))
        self.cache.delete_by_prefix(campaign_list_prefix())
        if org_id is not None:
            self.cache.delete(campaign_weight_presets_key(org_id))

    def campaign_candidates(self, campaign_id: Any, resume_ids: Iterable[Any] = ()) -> None:
        """
        A candidate in a campaign was added/moved/rescored/removed: campaign
        counts and flags, plus every resume/candidate view showing its stage.
        """
        self.campaign(campaign_id)
        self.resumes(resume_ids)

    def skills(self, skill_id: Any = None) -> None:
        """Skill detail/catalogs - including the catalog SkillNormalizationService matches against."""
        if not self.cache:
            return
        if skill_id is not None:
            self.cache.delete(skill_key(skill_id))
        self.cache.delete(
            skill_dashboard_summary_key(),
            skill_categories_key(),
            skill_catalog_key(),
            skill_alias_catalog_key(),
        )
        self.cache.delete_by_prefix(skill_prefix())
